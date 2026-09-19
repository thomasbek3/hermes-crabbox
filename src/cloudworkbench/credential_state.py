"""Private supervisor-only quarantine; never mount this state into a job."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat
import uuid

from .adapters import _open_directory_nofollow, read_credential


class CredentialStateError(ValueError):
    code = 'credential_state_invalid'


class CredentialState:
    """One startup identity; replacement requires a new supervisor instance."""
    def __init__(self, path: Path, startup_token: bytes):
        self.path = Path(path)
        self._fingerprint = hashlib.sha256(startup_token).hexdigest() if startup_token else None
        self._startup_reason = (None if startup_token.startswith(b'sk-ant-oat') and len(startup_token) <= 4096
                                and not any(c <= 32 or c >= 127 for c in startup_token)
                                else 'auth_missing' if not startup_token else 'auth_invalid')

    def _parent(self, create=False):
        if create:
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        parent = _open_directory_nofollow(self.path.parent)
        info = os.fstat(parent)
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) not in (0o700, 0o2700):
            os.close(parent)
            raise CredentialStateError('Private credential state directory required')
        return parent

    def _load(self):
        parent = self._parent()
        try:
            try:
                fd = os.open(self.path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            except FileNotFoundError as exc:
                raise CredentialStateError('Private credential state missing') from exc
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > 32768:
                    raise CredentialStateError('Invalid private credential state')
                raw = os.read(fd,32769)
                value = json.loads(raw)
                if not isinstance(value,dict) or value.get('schema_version') != 1 or not isinstance(value.get('quarantined'),dict):
                    raise CredentialStateError('Invalid private credential state')
                entries = value['quarantined']
                if len(entries) > 64 or any(not re.fullmatch('[a-f0-9]{64}',key) or reason != 'provider_auth_rejected' for key,reason in entries.items()):
                    raise CredentialStateError('Invalid private credential state')
                return entries
            finally:
                os.close(fd)
        finally:
            os.close(parent)

    @property
    def collection_block_name(self):
        return self.path.name + '.collection-block'

    def _collection_blocked(self, parent):
        try:
            fd = os.open(self.collection_block_name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        except FileNotFoundError:
            return False
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > 1024:
                raise CredentialStateError('Invalid collection block')
            if json.loads(os.read(fd, 1025)) != {'schema_version':1,'reason':'attempt_credential_unavailable'}:
                raise CredentialStateError('Invalid collection block')
            return True
        finally:
            os.close(fd)

    def block_collection(self):
        """Missing redaction identity blocks the provider, never guesses a token."""
        parent = self._parent()
        temporary = '.collection-block-' + uuid.uuid4().hex
        try:
            if self._collection_blocked(parent):
                return
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
            with os.fdopen(fd, 'w') as output:
                json.dump({'schema_version':1,'reason':'attempt_credential_unavailable'}, output)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.collection_block_name, src_dir_fd=parent, dst_dir_fd=parent)
            os.fsync(parent)
        finally:
            try: os.unlink(temporary, dir_fd=parent)
            except FileNotFoundError: pass
            os.close(parent)

    def clear_collection_block(self, *, operator_confirmed=False):
        """Explicit recovery after reviewing lost staging; never replays an attempt."""
        if operator_confirmed is not True:
            raise CredentialStateError('Explicit operator recovery required')
        parent = self._parent()
        try:
            if self._collection_blocked(parent):
                os.unlink(self.collection_block_name, dir_fd=parent)
                os.fsync(parent)
        finally:
            os.close(parent)

    def blocked_reason(self, token_path: Path | None = None):
        if token_path is not None:
            token, reason = read_credential(token_path)
            if reason:
                return reason
            if hashlib.sha256(token).hexdigest() != self._fingerprint:
                return 'auth_changed_restart_required'
        if self._startup_reason:
            return self._startup_reason
        try:
            parent = self._parent()
            try:
                if self._collection_blocked(parent):
                    return 'attempt_credential_unavailable'
            finally:
                os.close(parent)
            entries = self._load()
            if len(entries) >= 64 and self._fingerprint not in entries:
                return 'credential_state_invalid'
            return entries.get(self._fingerprint)
        except (OSError,ValueError,TypeError):
            return 'credential_state_invalid'

    def _write(self, parent, entries, *, create_only=False):
        temporary = '.credential-state-' + uuid.uuid4().hex
        fd = os.open(temporary,os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,0o600,dir_fd=parent)
        try:
            with os.fdopen(fd,'w') as output:
                json.dump({'schema_version':1,'quarantined':entries},output)
                output.flush()
                os.fsync(output.fileno())
            if create_only:
                os.link(temporary,self.path.name,src_dir_fd=parent,dst_dir_fd=parent,follow_symlinks=False)
            else:
                os.replace(temporary,self.path.name,src_dir_fd=parent,dst_dir_fd=parent)
            os.fsync(parent)
        finally:
            try:os.unlink(temporary,dir_fd=parent)
            except FileNotFoundError:pass

    def initialize(self):
        """Explicit provisioning only; normal supervisor startup never initializes."""
        parent = self._parent(create=True)
        try:
            try:self._write(parent,{},create_only=True)
            except FileExistsError:self._load()
        finally:os.close(parent)

    def clear_quarantine(self, *, operator_confirmed=False):
        """Trusted operator recovery only, after reviewing/revalidating the token."""
        if operator_confirmed is not True:
            raise CredentialStateError('Explicit operator recovery required')
        parent = self._parent()
        try:
            entries = self._load()
            entries.pop(self._fingerprint,None)
            self._write(parent,entries)
        finally:os.close(parent)

    def quarantine(self, reason: str):
        if reason != 'provider_auth_rejected' or self._startup_reason:
            raise CredentialStateError('Only a valid startup identity and terminal auth rejection can be quarantined')
        parent = self._parent()
        try:
            entries = self._load()
            if self._fingerprint not in entries and len(entries) >= 64:
                raise CredentialStateError('Private credential quarantine capacity reached')
            entries[self._fingerprint] = reason
            self._write(parent,entries)
        finally:os.close(parent)
