"""Worker-only dedicated auth staging. No discovery, refresh, or lease release.

This helper handles secret bytes in the trusted worker; never instantiate it in
the HTTP relay or expose its paths/identities as client-selected parameters.
"""
from contextlib import contextmanager
from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
import stat
import uuid

from .adapters import _open_directory_nofollow
from .native_responses import NativeError, NativeProfile
from .provider_bootstrap import _read_native_auth
from .provider_leases import LeaseError, ProviderLeases, RequestLease, Reservation, _id

_LIMIT = 65536


class SnapshotError(LeaseError):
    pass


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':')).encode()


def _identity(info):
    return [info.st_dev, info.st_ino, info.st_uid, info.st_gid,
            stat.S_IMODE(info.st_mode), info.st_nlink, info.st_size,
            info.st_mtime_ns, info.st_ctime_ns]


@dataclass(frozen=True)
class AuthSnapshot:
    """Non-bearer controller metadata; mount_path must reauthorize before use."""
    binding_digest: str
    path: Path


class AuthSnapshots:
    def __init__(self, leases, *, source: Path, dedicated_root: Path,
                 destination_root: Path, profile: NativeProfile,
                 source_uid: int, provider_gid: int, account_id: str,
                 persistent_owner_id: str):
        if not isinstance(leases, ProviderLeases) or type(profile) is not NativeProfile:
            raise SnapshotError('invalid_snapshot_configuration')
        paths = (source, dedicated_root, destination_root)
        if any(type(p) is not type(Path()) or not p.is_absolute() or '..' in p.parts for p in paths):
            raise SnapshotError('invalid_snapshot_path')
        # Controller-pinned dedicated service state only; no home discovery.
        # This common-prefix guard is not exhaustive filesystem classification.
        if any(p.parts[1:2] in [('home',), ('Users',), ('root',)] for p in paths):
            raise SnapshotError('personal_home_forbidden')
        if (dedicated_root not in source.parents or source == destination_root
                or dedicated_root == destination_root
                or dedicated_root in destination_root.parents
                or destination_root in dedicated_root.parents):
            raise SnapshotError('invalid_snapshot_path')
        if any(type(v) is not int or v < 0 for v in (source_uid, provider_gid)):
            raise SnapshotError('invalid_snapshot_configuration')
        if source_uid != os.geteuid():
            raise SnapshotError('dedicated_worker_required')
        if os.geteuid() != 0 and provider_gid != os.getegid() and provider_gid not in os.getgroups():
            raise SnapshotError('provider_group_unavailable')
        self.leases, self.source, self.dedicated_root = leases, source, dedicated_root
        self.root, self.profile = destination_root, profile
        self.uid, self.gid = source_uid, provider_gid
        self.account_id, self.owner_id = _id(account_id), _id(persistent_owner_id)

    def _binding(self, lease, attempt_id, generation):
        if (type(lease) is not RequestLease or type(lease.reservation) is not Reservation
                or lease.purpose != 'inference'):
            raise SnapshotError('inference_lease_required')
        if (lease.reservation.account_id != self.account_id
                or lease.reservation.persistent_owner_id != self.owner_id):
            raise SnapshotError('snapshot_account_mismatch')
        _id(attempt_id)
        if type(generation) is not int or generation < 1:
            raise SnapshotError('invalid_generation')
        binding = {'version': 1, 'lease': asdict(lease), 'attempt_id': attempt_id,
                   'generation': generation, 'profile_digest': self.profile.digest,
                   'source': str(self.source), 'source_uid': self.uid,
                   'provider_gid': self.gid, 'destination_root': str(self.root)}
        digest = hashlib.sha256(_json(binding)).hexdigest()
        return binding, digest

    @contextmanager
    def _root(self, path):
        fd = _open_directory_nofollow(path)
        try:
            info = os.fstat(fd)
            if info.st_uid != self.uid or stat.S_IMODE(info.st_mode) & 0o777 != 0o700:
                raise SnapshotError('unsafe_snapshot_directory')
            # The parent chain must not be writable by another Unix identity.
            for parent in path.parents:
                meta = parent.lstat()
                sticky_root = meta.st_uid == 0 and bool(meta.st_mode & stat.S_ISVTX)
                if (not stat.S_ISDIR(meta.st_mode) or meta.st_uid not in (0, self.uid)
                        or meta.st_mode & 0o022 and not sticky_root):
                    raise SnapshotError('unsafe_snapshot_parent')
            yield fd
        finally:
            os.close(fd)

    def _authorized(self, db, lease, attempt_id, generation):
        request = self.leases._request(db, lease)
        self.leases._reservation(db, lease.reservation, held=True)
        grant = db.execute('SELECT * FROM provider_execution_grants WHERE id=?',
                           (lease.grant_id,)).fetchone()
        now = self.leases._now()
        if (request['state'] != 'held' or now >= request['expires_at']
                or not grant or grant['attempt_id'] != attempt_id
                or grant['generation'] != generation or grant['revoked_at'] is not None
                or now >= grant['expires_at']
                or not self.leases._attempt_valid(db, attempt_id, generation)
                or not self.leases._workflow_grant_valid(db, lease.reservation.reservation_id, attempt_id)):
            raise SnapshotError('snapshot_not_authorized')
        dispatch = db.execute('SELECT * FROM provider_dispatch WHERE request_id=?',
                              (lease.request_id,)).fetchone()
        if dispatch and (dispatch['reservation_id'] != lease.reservation.reservation_id
                         or dispatch['attempt_id'] != attempt_id or dispatch['generation'] != generation
                         or dispatch['profile_digest'] != self.profile.digest
                         or dispatch['cancel_requested'] or dispatch['uncertain']
                         or dispatch['state'] not in ('admitted', 'create_intent', 'created', 'start_intent')):
            raise SnapshotError('snapshot_dispatch_mismatch')

    def _source_bytes(self):
        with self._root(self.dedicated_root):
            parent = _open_directory_nofollow(self.source.parent)
            try:
                current = self.source.parent
                while current != self.dedicated_root:
                    meta = current.lstat()
                    if meta.st_uid != self.uid or meta.st_mode & 0o022:
                        raise SnapshotError('unsafe_auth_source')
                    current = current.parent
                fd = os.open(self.source.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            finally:
                os.close(parent)
            try:
                before = os.fstat(fd)
                if (not stat.S_ISREG(before.st_mode) or before.st_uid != self.uid
                        or before.st_mode & 0o077 or before.st_nlink != 1
                        or not 0 < before.st_size <= _LIMIT):
                    raise SnapshotError('unsafe_auth_source')
                data = bytearray()
                while len(data) <= _LIMIT:
                    chunk = os.read(fd, min(8192, _LIMIT + 1 - len(data)))
                    if not chunk:
                        break
                    data.extend(chunk)
                current = self.source.lstat()
                if (len(data) != before.st_size or _identity(before) != _identity(os.fstat(fd))
                        or _identity(before) != _identity(current)):
                    raise SnapshotError('auth_source_changed')
                return data
            finally:
                os.close(fd)

    @staticmethod
    def _write(folder, name, data, *, gid=None):
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=folder)
        try:
            view = memoryview(data)
            while view:
                count = os.write(fd, view)
                if count <= 0:
                    raise SnapshotError('snapshot_write_failed')
                view = view[count:]
            if gid is not None:
                os.fchown(fd, -1, gid)
                os.fchmod(fd, 0o440)
            os.fsync(fd)
            return _identity(os.fstat(fd))
        finally:
            os.close(fd)

    def prepare(self, lease, *, attempt_id, generation):
        """Publish once. Call under the encompassing dispatch account lock."""
        binding, digest = self._binding(lease, attempt_id, generation)
        temporary = '.pending-' + uuid.uuid4().hex
        try:
            with self.leases.account_lock(lease.reservation.account_id), self._root(self.root) as root:
                # Validate before touching any credential bytes.
                with self.leases.store._tx() as db:
                    self._authorized(db, lease, attempt_id, generation)
                try:
                    os.stat(digest, dir_fd=root, follow_symlinks=False)
                except FileNotFoundError:
                    pass
                else:
                    raise SnapshotError('snapshot_already_exists')
                data = self._source_bytes()
                published = False; created = False; folder = None
                try:
                    os.mkdir(temporary, 0o700, dir_fd=root)
                    created = True
                    folder = os.open(temporary, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root)
                    self._write(folder, 'auth.json', data)
                    # Existing exact CLI schema + expiry validator. Returned secret
                    # values stay in this process and are never placed in receipts.
                    _read_native_auth(self.profile, self.root / temporary / 'auth.json')
                    fd = os.open('auth.json', os.O_RDONLY | os.O_NOFOLLOW, dir_fd=folder)
                    try:
                        os.fchown(fd, -1, self.gid)
                        os.fchmod(fd, 0o440)
                        os.fsync(fd)
                        identity = _identity(os.fstat(fd))
                    finally:
                        os.close(fd)
                    self._write(folder, 'binding.json', _json({'binding': binding, 'file': identity}))
                    os.fsync(folder)
                    with self.leases.store._tx() as db:
                        self._authorized(db, lease, attempt_id, generation)
                        os.rename(temporary, digest, src_dir_fd=root, dst_dir_fd=root)
                        published = True
                        os.fsync(root)
                    return AuthSnapshot(digest, self.root / digest / 'auth.json')
                finally:
                    for i in range(len(data)):
                        data[i] = 0
                    try:
                        if not published and created:
                            # If opening failed, our new directory is still empty.
                            # Otherwise reuse the existing fd even under EMFILE.
                            if folder is not None:
                                for name in ('auth.json', 'binding.json'):
                                    try: os.unlink(name, dir_fd=folder)
                                    except FileNotFoundError: pass
                            os.rmdir(temporary, dir_fd=root)
                    finally:
                        if folder is not None: os.close(folder)
        except NativeError as exc:
            raise SnapshotError(str(exc) if str(exc).startswith('auth_') else 'auth_invalid') from None
        except OSError:
            raise SnapshotError('snapshot_io_failed') from None

    def _check_snapshot(self, root, digest, binding, *, allow_removed_auth=False):
        folder = os.open(digest, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root)
        try:
            meta = os.fstat(folder)
            if meta.st_uid != self.uid or stat.S_IMODE(meta.st_mode) & 0o777 != 0o700:
                raise SnapshotError('snapshot_identity_changed')
            fd = os.open('binding.json', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=folder)
            try:
                info = os.fstat(fd)
                if (not stat.S_ISREG(info.st_mode) or info.st_uid != self.uid or info.st_nlink != 1
                        or stat.S_IMODE(info.st_mode) != 0o600 or not 0 < info.st_size <= 16384):
                    raise SnapshotError('snapshot_identity_changed')
                receipt = json.loads(os.read(fd, 16385))
            finally: os.close(fd)
            if (type(receipt) is not dict or set(receipt) != {'binding', 'file'}
                    or receipt['binding'] != binding or type(receipt['file']) is not list
                    or len(receipt['file']) != 9 or any(type(v) is not int for v in receipt['file'])):
                raise SnapshotError('snapshot_identity_changed')
            try: info = os.stat('auth.json', dir_fd=folder, follow_symlinks=False)
            except FileNotFoundError:
                if allow_removed_auth:
                    return folder
                raise
            if (not stat.S_ISREG(info.st_mode) or receipt != {'binding': binding, 'file': _identity(info)}
                    or info.st_uid != self.uid or info.st_gid != self.gid
                    or stat.S_IMODE(info.st_mode) != 0o440 or info.st_nlink != 1):
                raise SnapshotError('snapshot_identity_changed')
            return folder
        except BaseException:
            os.close(folder)
            raise

    def mount_path(self, lease, *, attempt_id, generation):
        """Metadata-only check immediately before Docker create; no source read."""
        binding, digest = self._binding(lease, attempt_id, generation)
        try:
            with self.leases.account_lock(lease.reservation.account_id), self._root(self.root) as root:
                with self.leases.store._tx() as db:
                    self._authorized(db, lease, attempt_id, generation)
                    os.close(self._check_snapshot(root, digest, binding))
                return self.root / digest / 'auth.json'
        except (OSError, ValueError, RecursionError) as exc:
            if isinstance(exc, LeaseError): raise
            raise SnapshotError('snapshot_identity_changed') from None

    def expected_path(self, lease, *, attempt_id, generation):
        """Pure metadata for recovery mount comparison; grants no file access."""
        _, digest = self._binding(lease, attempt_id, generation)
        return self.root / digest / 'auth.json'

    def cleanup(self, lease, *, attempt_id, generation):
        """Delete exact snapshot only after trusted durable cleanup released lease."""
        binding, digest = self._binding(lease, attempt_id, generation)
        try:
            with self.leases.account_lock(lease.reservation.account_id), self._root(self.root) as root:
                with self.leases.store._tx() as db:
                    row = db.execute('''SELECT q.*,r.account_id,r.epoch,r.controller_instance_id,
                        a.persistent_owner_id,g.attempt_id,g.generation FROM provider_request_leases q
                        JOIN provider_reservations r ON r.id=q.reservation_id
                        JOIN provider_accounts a ON a.account_id=r.account_id
                        JOIN provider_execution_grants g ON g.id=q.grant_id WHERE q.id=?''', (lease.request_id,)).fetchone()
                    owner = lease.reservation
                    expected = (owner.reservation_id, lease.grant_id, lease.purpose, lease.expires_at,
                                owner.account_id, owner.epoch, owner.controller_instance_id,
                                owner.persistent_owner_id, attempt_id, generation)
                    columns = ('reservation_id','grant_id','purpose','expires_at','account_id','epoch',
                               'controller_instance_id','persistent_owner_id','attempt_id','generation')
                    if (not row or tuple(row[k] for k in columns) != expected
                            or row['state'] != 'released' or not row['cleanup_receipt']):
                        raise SnapshotError('snapshot_cleanup_unconfirmed')
                    try: folder = self._check_snapshot(root, digest, binding, allow_removed_auth=True)
                    except FileNotFoundError:
                        # Only a missing entire directory is an idempotent success.
                        try: os.stat(digest, dir_fd=root, follow_symlinks=False)
                        except FileNotFoundError: return
                        raise SnapshotError('snapshot_identity_changed') from None
                    try:
                        if set(os.listdir(folder)) not in ({'auth.json', 'binding.json'}, {'binding.json'}):
                            raise SnapshotError('snapshot_identity_changed')
                        try: os.unlink('auth.json', dir_fd=folder)
                        except FileNotFoundError: pass
                        os.unlink('binding.json', dir_fd=folder)
                        os.fsync(folder)
                    finally: os.close(folder)
                    os.rmdir(digest, dir_fd=root)
                    os.fsync(root)
        except (OSError, ValueError, RecursionError) as exc:
            if isinstance(exc, LeaseError): raise
            raise SnapshotError('snapshot_cleanup_failed') from None
