"""Run only inside a qualified disposable provider container.

Consumes a read-only dedicated setup-token file; never generates a login or
copies auth state back. Container placement/mounts/egress are controller duties.
"""
from dataclasses import asdict
from pathlib import Path

from .adapters import read_credential
from .inference_transport import InferenceTransport, TransportError


def _contains(value, secret):
    if isinstance(value, str):
        return secret in value
    if isinstance(value, dict):
        return any(_contains(key, secret) or _contains(item, secret) for key, item in value.items())
    if isinstance(value, (tuple, list)):
        return any(_contains(item, secret) for item in value)
    return False


def run_token_request(profile, request, *, home: Path, scratch: Path,
                      credential_path: Path = Path('/run/secrets/claude-token'),
                      proxy=None, limits=None, cancel=None):
    """Return a bounded public envelope; secret path is trusted configuration.

    This function does not establish container isolation, a lease, token validity,
    backend entitlement or physical cleanup. Every result requires outer cleanup.
    HOME/scratch must be private0700 subdirectories of ephemeral container tmpfs.
    Secret must be regular, no symlinks, one link, <=4096bytes, readable by the
    container UID; prefer0400/0600. Approved group-read0640 is accepted, group-write
    and any other-user access are rejected. Permission/content failures both use
    auth_invalid. Private tmpfs token persistence is not scanned or exported;
    exact-output detection is not protection against arbitrary transformed leaks.
    """
    def failure(code):
        return {'version': 1, 'status': 'error', 'error': code, 'result': None, 'container_cleanup_required': True,
                'credential_reuse_authorized': False}
    transport = None
    token = None
    try:
        transport = InferenceTransport(profile, home=home, scratch=scratch, proxy=proxy, limits=limits)
        raw, error = read_credential(credential_path)
        if error:
            return failure(error)
        token = raw.decode('ascii')
        transport.env.update({'CLAUDE_CODE_OAUTH_TOKEN': token, 'DISABLE_AUTOUPDATER': '1',
                              'CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC': '1'})
        result = transport.run(request, cancel=cancel)
        public = asdict(result)
        if _contains(public, token):
            return failure('credential_exposure_detected')
        error = None if result.status == 'ok' and result.error_code is None else (result.error_code or 'provider_failed')
        return {'version': 1, 'status': 'ok' if error is None else 'error', 'error': error,
                'result': public, 'container_cleanup_required': True,
                'credential_reuse_authorized': False}
    except TransportError as exc:
        # Codes originate in the trusted transport; never serialize raw CLI errors.
        return failure(exc.code)
    except Exception:
        return failure('provider_bootstrap_failed')
    finally:
        if transport is not None:
            transport.env.pop('CLAUDE_CODE_OAUTH_TOKEN', None)
        # Python strings cannot be reliably zeroed. Destroy the container before reuse.
        token = None


def _read_native_auth(profile, path):
    """Read only inside the disposable provider; unknown CLI schemas refuse."""
    import base64
    import json
    import os
    import re
    import stat
    import time
    from .adapters import _open_directory_nofollow
    from .native_responses import NativeError, _decode
    try:
        parent = _open_directory_nofollow(path.parent)
        try: fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        finally: os.close(parent)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_mode & 0o027
                    or not 0 < info.st_size <= 65536):
                raise NativeError('auth_invalid')
            data = bytearray()
            while len(data) <= 65536:
                chunk = os.read(fd, min(8192, 65537-len(data)))
                if not chunk: break
                data.extend(chunk)
            if len(data) > 65536: raise NativeError('auth_invalid')
        finally: os.close(fd)
    except FileNotFoundError: raise NativeError('auth_missing') from None
    except OSError: raise NativeError('auth_invalid') from None
    try:
        value = _decode(data)
        if profile.provider=='xai-oauth': return _parse_grok_auth(value)
        if profile.provider!='openai-codex': raise NativeError('auth_schema_unsupported')
        if (not isinstance(value,dict) or set(value)-{'auth_mode','OPENAI_API_KEY','tokens','last_refresh'}
                or value.get('auth_mode','chatgpt') != 'chatgpt' or value.get('OPENAI_API_KEY') is not None):
            raise NativeError('auth_schema_unsupported')
        tokens = value.get('tokens')
        if not isinstance(tokens,dict) or set(tokens)-{'access_token','refresh_token','id_token','account_id'}:
            raise NativeError('auth_schema_unsupported')
        access,refresh = tokens.get('access_token'),tokens.get('refresh_token')
        if any(not isinstance(v,str) or not 1<=len(v)<=16384 or any(ord(c)<33 or ord(c)>126 for c in v) for v in (access,refresh)):
            raise NativeError('auth_invalid')
        parts=access.split('.')
        if len(parts)!=3 or not all(re.fullmatch(r'[A-Za-z0-9_-]+',v) for v in parts): raise NativeError('auth_invalid')
        claims=_decode(base64.urlsafe_b64decode(parts[1]+'='*(-len(parts[1])%4)))
        exp=claims.get('exp') if isinstance(claims,dict) else None
        if type(exp) not in (int,float) or not 0<exp<1e12: raise NativeError('auth_invalid')
        if exp <= time.time()+120: raise NativeError('auth_expired')
        account=tokens.get('account_id')
        namespace=claims.get('https://api.openai.com/auth',{})
        if not isinstance(namespace,dict): raise NativeError('auth_invalid')
        claimed=namespace.get('chatgpt_account_id')
        if account is not None and claimed is not None and account!=claimed: raise NativeError('auth_invalid')
        account=account or claimed
        if account is not None and (not isinstance(account,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,256}',account)):
            raise NativeError('auth_invalid')
        secrets=[v for k,v in tokens.items() if k in ('access_token','refresh_token','id_token') and isinstance(v,str) and v]
        return access,account,secrets
    except NativeError as exc:
        if str(exc).startswith('auth_'): raise
        raise NativeError('auth_invalid') from None
    except Exception: raise NativeError('auth_invalid') from None



def _parse_grok_auth(value):
    """Observed dedicated Grok1.0.34 OIDC file namespace, not an API URL."""
    from datetime import datetime, timezone
    import time
    from .native_responses import NativeError
    issuer='https://auth.x.ai'
    client='b1a00492-073a-47ea-816f-4c329264a828'
    namespace=issuer+'::'+client
    if type(value) is not dict or set(value)!={namespace}: raise NativeError('auth_schema_unsupported')
    record=value[namespace]
    allowed={'key','auth_mode','create_time','user_id','email','first_name','profile_image_asset_id',
        'principal_type','principal_id','team_id','coding_data_retention_opt_out',
        'refresh_token','expires_at','oidc_issuer','oidc_client_id'}
    if (type(record) is not dict or set(record)-allowed or record.get('auth_mode')!='oidc'
            or record.get('oidc_issuer')!=issuer or record.get('oidc_client_id')!=client):
        raise NativeError('auth_schema_unsupported')
    access,refresh=record.get('key'),record.get('refresh_token')
    if any(not isinstance(v,str) or not 1<=len(v)<=16384 or any(ord(c)<33 or ord(c)>126 for c in v) for v in (access,refresh)):
        raise NativeError('auth_invalid')
    try:
        stamp=record['expires_at']
        if not isinstance(stamp,str): raise ValueError()
        expiry=datetime.fromisoformat(stamp.replace('Z','+00:00'))
        if expiry.tzinfo is None: raise ValueError()
        remaining=expiry.astimezone(timezone.utc).timestamp()-time.time()
    except (KeyError,TypeError,ValueError,OverflowError): raise NativeError('auth_invalid') from None
    if remaining<=120: raise NativeError('auth_expired')
    return access,None,[access,refresh]


def run_native_request(profile, request, *, credential_path=Path('/run/secrets/native-auth.json'), proxy=None, cancel=None, transport_factory=None):
    """Native HTTP only; no CLI execution, refresh, credential discovery or copyback."""
    from .native_responses import NativeError, NativeProfile, NativeResponses
    def failure(code):
        return {'version':1,'status':'error','error':code,'result':None,
                'container_cleanup_required':True,'credential_reuse_authorized':False}
    safe_errors={'auth_missing','auth_invalid','auth_schema_unsupported','auth_expired','provider_auth_rejected',
        'rate_limited','cancelled','wall_timeout','transport_error','credential_exposure_detected',
        'transport_cleanup_unconfirmed'}
    material=None;access=None
    try:
        if type(profile) is not NativeProfile: return failure('invalid_native_profile')
        transport=(transport_factory or NativeResponses)(profile,proxy=proxy)
        access,account,material=_read_native_auth(profile,credential_path)
        result=transport.execute(request,credential=access,account_id=account,cancel=cancel)
        if result.status!='ok': return failure(result.error if result.error in safe_errors else 'native_provider_failed')
        if not result.transport_stopped: return failure('transport_cleanup_unconfirmed')
        if type(result.response) is not dict: return failure('invalid_native_response')
        public={'profile_digest':profile.digest,'response':result.response,'transport_stopped':True}
        if any(_contains(public,secret) for secret in material): return failure('credential_exposure_detected')
        return {'version':1,'status':'ok','error':None,'result':public,
                'container_cleanup_required':True,'credential_reuse_authorized':False}
    except NativeError as exc:
        return failure(str(exc) if str(exc) in safe_errors else 'native_provider_failed')
    except Exception: return failure('native_provider_bootstrap_failed')
    finally:
        # Strings cannot be reliably zeroed; the enclosing container must die.
        material=None;access=None
