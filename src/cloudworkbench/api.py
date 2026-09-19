"""Authenticated API. Job execution is exclusively owned by the runner."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import stat
import sqlite3
import tempfile
import time
import threading
from fastapi import FastAPI, Depends, Header, Request, Query
from fastapi.responses import JSONResponse, StreamingResponse, Response
from starlette.concurrency import run_in_threadpool
from .models import SessionRequest, MessageRequest, InputRequest, TERMINAL
from .store import StoreError
from .result_bundle import BundleError, authorized_snapshot, build_bundle, latest_root_attempt
from .environments import EnvironmentRegistry, EnvironmentError
from .dashboard import mount_dashboard, principal_from_cookie


def public(value):
    if isinstance(value, dict):
        return {k: public(v) for k, v in value.items() if k not in {'storage_path', 'token_hash'}}
    if isinstance(value, list):
        return [public(v) for v in value]
    return value


class BodyLimit:
    def __init__(self, app, json_limit=1048576, upload_limit=104857600):
        self.app, self.json_limit, self.upload_limit = app, json_limit, upload_limit

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        is_upload = scope['method'] == 'PUT' and scope['path'].startswith('/v1/inputs/') and scope['path'].endswith('/content')
        limit = self.upload_limit if is_upload else self.json_limit
        headers = dict(scope['headers'])
        try:
            length = int(headers.get(b'content-length', b'0'))
        except ValueError:
            return await JSONResponse({'detail': 'Invalid content length'}, status_code=400)(scope, receive, send)
        if length < 0 or length > limit:
            return await JSONResponse({'detail': 'Request too large'}, status_code=413)(scope, receive, send)
        total = 0
        async def limited_receive():
            nonlocal total
            message = await receive()
            total += len(message.get('body', b''))
            if total > limit:
                raise StoreError(413, 'Request too large')
            return message
        await self.app(scope, limited_receive, send)


def create_app(store, settings=None):
    settings = settings or {}
    app = FastAPI(title='Cloud Workbench', docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(BodyLimit, upload_limit=settings.get('input_max_bytes', 104857600))
    rate_buckets = {}
    rate_lock = threading.Lock()

    @app.exception_handler(StoreError)
    async def store_error(request, exc):
        return JSONResponse({'detail': exc.detail}, status_code=exc.status_code, headers={'Retry-After':'5'} if exc.status_code in {429, 503} else None)

    @app.middleware('http')
    async def security_headers(request, call_next):
        response = await call_next(request)
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Cache-Control'] = 'no-store'
        response.headers.setdefault('Content-Security-Policy', "default-src 'none'; frame-ancestors 'none'; sandbox")
        return response

    def principal(request: Request, authorization: str | None = Header(default=None)):
        if authorization is None:
            return principal_from_cookie(request)
        if not authorization.startswith('Bearer ') or len(authorization) > 1024:
            raise StoreError(401, 'Bearer authentication required')
        value = store.authenticate(authorization[7:])
        if value is None:
            raise StoreError(401, 'Invalid credential')
        return value

    def mutation(p=Depends(principal), idempotency_key: str = Header(alias='Idempotency-Key', min_length=1, max_length=200)):
        # Bounded in-process admission protects the single controller; state mutations remain durable.
        clock = time.monotonic()
        with rate_lock:
            bucket = rate_buckets.setdefault(p['id'], [])
            bucket[:] = [x for x in bucket if x > clock - 60]
            if len(bucket) >= settings.get('mutations_per_minute', 120):
                raise StoreError(429, 'Mutation rate exceeded')
            bucket.append(clock)
        return p, idempotency_key

    def validate_config(request):
        projects = settings.get('projects', {})
        config = projects.get(request.project_id) if isinstance(projects, dict) else None
        if config is None:
            raise StoreError(422, 'Project is not configured')
        agents = config.get('allowed_agents', [])
        if request.agent not in agents:
            raise StoreError(422, 'Adapter is not allowed for project')
        if request.agent == 'hermes':
            if settings.get('hermes_enabled') is not True:
                raise StoreError(503, 'Hermes adapter is not qualified')
            if request.model != 'grok-4.6':
                raise StoreError(422, 'Hermes requires its qualified model')
        if request.agent == 'fixture' and not settings.get('test_mode', False):
            raise StoreError(422, 'Fixture adapter is test-only')
        models = config.get('models', {})
        if not isinstance(models, dict):
            raise StoreError(422, 'Project model policy must map adapters to allowed models')
        allowed_models = models.get(request.agent, [])
        if request.model is not None and request.model not in allowed_models:
            raise StoreError(422, 'Model is not configured')
        if request.environment_version is None or request.environment_version not in config.get('environment_versions', []):
            raise StoreError(422, 'Explicit configured environment version required')
        if settings.get('environment_registry'):
            try:
                EnvironmentRegistry(settings['environment_registry'], read_only=True).resolve(
                    request.project_id, request.environment_version,
                    operator_approved_sha256=config.get('operator_approved_environments', {}).get(request.environment_version))
            except (EnvironmentError, OSError, ValueError, sqlite3.Error):
                raise StoreError(422, 'Environment version is unavailable or not qualified') from None

    @app.get('/health')
    def health():
        return {'status': 'alive'}

    @app.get('/v1/health')
    def v1_health():
        return {'status': 'alive'}

    @app.get('/v1/ready')
    def ready(p=Depends(principal)):
        callback = settings.get('readiness')
        result = callback() if callback else {'ready': False, 'reason': 'Worker readiness unconfigured'}
        return JSONResponse(public(result), status_code=200 if result.get('ready') else 503)

    @app.get('/v1/capabilities')
    def capabilities(p=Depends(principal)):
        return {'capabilities': public(settings.get('capabilities', {})), 'unsupported': ['takeover', 'release', 'approvals', 'purge'] + ([] if settings.get('dashboard_origin') else ['dashboard_cookie_auth']), 'input_max_bytes': settings.get('input_max_bytes', 104857600)}

    @app.post('/v1/sessions', status_code=201)
    def create(body: SessionRequest, auth=Depends(mutation)):
        validate_config(body)
        return store.create_session(auth[0], body.model_dump(), auth[1])

    @app.get('/v1/sessions')
    def listing(p=Depends(principal), limit: int=Query(50, ge=1, le=100), offset: int=Query(0, ge=0)):
        return {'sessions': store.list_sessions(p, limit, offset)}

    @app.get('/v1/sessions/{session_id}')
    def detail(session_id: str, p=Depends(principal)):
        return public(store.get_session(p, session_id))

    @app.post('/v1/sessions/{session_id}/messages')
    def message(session_id: str, body: MessageRequest, auth=Depends(mutation)):
        return store.add_message(auth[0], session_id, body.message, auth[1])

    @app.post('/v1/sessions/{session_id}/resume')
    def resume(session_id: str, auth=Depends(mutation)):
        return store.resume(auth[0], session_id, auth[1])

    @app.post('/v1/attempts/{attempt_id}/cancel')
    def cancel(attempt_id: str, auth=Depends(mutation)):
        return public(store.cancel(auth[0], attempt_id, auth[1]))

    @app.post('/v1/sessions/{session_id}/archive')
    def archive(session_id: str, auth=Depends(mutation)):
        return store.archive(auth[0], session_id, auth[1])

    @app.get('/v1/sessions/{session_id}/events')
    async def events(session_id: str, request: Request, p=Depends(principal), after: int=Query(0, ge=0), follow: bool=True, last_event_id: str | None=Header(default=None)):
        if last_event_id is not None:
            try:
                after = int(last_event_id)
            except ValueError:
                raise StoreError(422, 'Invalid Last-Event-ID')
        initial = await run_in_threadpool(store.events, p, session_id, after=after)
        async def stream():
            cursor, batch = after, initial
            deadline = time.monotonic() + settings.get('sse_max_seconds', 300)
            while True:
                for event in batch:
                    cursor = event['sequence']
                    yield f"id: {cursor}\nevent: {event['type']}\ndata: {json.dumps(public(event), separators=(',', ':'))}\n\n"
                if not follow or await request.is_disconnected() or time.monotonic() >= deadline:
                    break
                state = await run_in_threadpool(store.get_session, p, session_id)
                if all(a['state'] in TERMINAL for a in state['attempts']):
                    final_batch = await run_in_threadpool(store.events, p, session_id, after=cursor)
                    if final_batch:
                        batch = final_batch
                        continue
                    break
                yield ': keepalive\n\n'
                await asyncio.sleep(settings.get('sse_poll_seconds', 1))
                batch = await run_in_threadpool(store.events, p, session_id, after=cursor)
        return StreamingResponse(stream(), media_type='text/event-stream', headers={'X-Accel-Buffering':'no'})

    @app.get('/v1/sessions/{session_id}/bundle')
    def result_bundle(session_id: str, p=Depends(principal)):
        try:
            session, artifacts = authorized_snapshot(store, p, session_id)
            root = settings.get('artifact_root')
            if not root:
                raise StoreError(503, 'Artifact storage unavailable')
            content = build_bundle(session, artifacts, root)
        except BundleError as exc:
            raise StoreError(exc.status_code, exc.detail) from exc
        attempt = session['attempts'][0]
        digest = hashlib.sha256(content).hexdigest()
        return Response(content, media_type='application/zip', headers={
            'Content-Disposition': f'attachment; filename="session-{session_id}-attempt-{attempt["id"]}.zip"',
            'ETag': '"' + digest + '"', 'Content-Length': str(len(content)),
            'X-Cloud-Session-Id': session_id, 'X-Cloud-Attempt-Id': attempt['id']})

    @app.get('/v1/sessions/{session_id}/artifacts')
    def artifacts(session_id: str, p=Depends(principal)):
        return {'artifacts': public(store.list_artifacts(p, session_id))}

    @app.get('/v1/artifacts/{artifact_id}/content')
    def download(artifact_id: str, p=Depends(principal)):
        artifact = store.get_artifact(p, artifact_id)
        root = settings.get('artifact_root')
        if not root:
            raise StoreError(503, 'Artifact storage unavailable')
        root = Path(root).absolute()
        path = Path(artifact['storage_path'])
        try:
            relative = path.relative_to(root) if path.is_absolute() else path
            if any(part in {'..', '.'} for part in relative.parts) or not relative.parts:
                raise ValueError()
            directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                for part in relative.parts[:-1]:
                    child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
                    os.close(directory)
                    directory = child
                fd = os.open(relative.parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            finally:
                os.close(directory)
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size != artifact['bytes']:
                os.close(fd)
                raise ValueError()
        except (OSError, ValueError):
            raise StoreError(404, 'Artifact content unavailable')
        def content():
            try:
                while chunk := os.read(fd, 65536):
                    yield chunk
            finally:
                os.close(fd)
        # Never render untrusted HTML/SVG inline under the controller origin.
        return StreamingResponse(content(), media_type='application/octet-stream', headers={'Content-Disposition': f'attachment; filename="{artifact_id}.bin"', 'Content-Length': str(info.st_size), 'ETag': '"' + artifact['sha256'] + '"'})

    @app.post('/v1/inputs', status_code=201)
    def reserve(body: InputRequest, auth=Depends(mutation)):
        return store.reserve_input(auth[0], body.model_dump(), auth[1])

    @app.put('/v1/inputs/{input_id}/content')
    async def upload(input_id: str, request: Request, auth=Depends(mutation)):
        p, key = auth
        await run_in_threadpool(store.get_input, p, input_id)
        root = settings.get('input_root')
        if not root:
            raise StoreError(503, 'Input storage unavailable')
        root = Path(root)
        def stage_upload():
            root.mkdir(parents=True, exist_ok=True, mode=0o2770)
            if root.is_symlink() or not root.is_dir():
                raise StoreError(503, 'Input storage directory unavailable')
            return tempfile.mkstemp(prefix='upload-', dir=root)
        fd, name = await run_in_threadpool(stage_upload)
        digest, count = hashlib.sha256(), 0
        published = False
        try:
            with os.fdopen(fd, 'wb') as output:
                async for chunk in request.stream():
                    count += len(chunk)
                    if count > settings.get('input_max_bytes', 104857600):
                        raise StoreError(413, 'Input exceeds byte limit')
                    def write_chunk():
                        output.write(chunk)
                        digest.update(chunk)
                    await run_in_threadpool(write_chunk)
                await run_in_threadpool(output.flush)
                await run_in_threadpool(os.fsync, output.fileno())
            await run_in_threadpool(os.chmod, name, 0o440)
            result = await run_in_threadpool(store.finalize_input, p, input_id, {'storage_path': str(Path(name).absolute()), 'sha256':digest.hexdigest(), 'bytes':count}, key=key)
            published = result['storage_path'] == str(Path(name).absolute())
            return public(result)
        finally:
            if not published:
                await run_in_threadpool(Path(name).unlink, missing_ok=True)

    @app.get('/v1/sessions/{session_id}/diff')
    def delivery_diff(session_id: str, p=Depends(principal)):
        session = store.get_session(p, session_id)
        try:
            attempt = latest_root_attempt(session.get('attempts', []))
        except BundleError as exc:
            raise StoreError(exc.status_code, str(exc)) from None
        if attempt is not None and attempt['state'] not in TERMINAL:
            raise StoreError(409, 'Latest attempt is not terminal')
        if attempt is None or not (attempt.get('result') or {}).get('delivery'):
            raise StoreError(409, 'No repository delivery exists for the latest attempt')
        artifacts = store.list_artifacts(p, session_id)
        patch = next((item for item in artifacts if item['attempt_id'] == attempt['id'] and item['path'] == '@delivery/changes.patch'), None)
        if patch is None:
            raise StoreError(409, 'Repository delivery artifact is unavailable')
        return public({'attempt_id':attempt['id'], **attempt['result']['delivery'], 'patch_artifact_id':patch['id']})

    @app.get('/v1/sessions/{session_id}/retention')
    @app.get('/v1/sessions/{session_id}/retention/dry-run')
    def retention_plan(session_id: str, p=Depends(principal)):
        return public(store.get_retention(p, session_id))

    @app.patch('/v1/sessions/{session_id}/retention')
    def retention_control(session_id: str, body: dict, auth=Depends(mutation)):
        changes = dict(body)
        revision = changes.pop('expected_revision', None)
        return public(store.set_retention(auth[0], session_id, changes, revision, auth[1]))

    @app.api_route('/v1/sessions/{session_id}/{feature}', methods=['POST', 'GET'])
    def unavailable(session_id: str, feature: str, p=Depends(principal)):
        store.get_session(p, session_id)
        if feature not in {'takeover', 'release', 'diff'}:
            raise StoreError(404, 'Endpoint not found')
        raise StoreError(501, f'{feature} is not supported in this release')

    @app.delete('/v1/sessions/{session_id}')
    def purge(session_id: str, auth=Depends(mutation)):
        store.get_session(auth[0], session_id)
        raise StoreError(501, 'Purge is unavailable until retention cleanup is verified; archive preserves data')

    @app.post('/v1/approvals/{approval_id}/decision')
    def approve(approval_id: str, auth=Depends(mutation)):
        raise StoreError(501, 'Approvals are not supported in this release')

    if settings.get('dashboard_origin'):
        mount_dashboard(app, store, settings)

    return app
