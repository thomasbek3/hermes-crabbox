"""Private browser session auth and self-contained workbench dashboard."""
from __future__ import annotations

from collections import deque
import hashlib
import hmac
import json
from pathlib import Path
import secrets
import sqlite3
import threading
import time
from urllib.parse import urlsplit

from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse
from .environments import EnvironmentRegistry

from .store import StoreError


COOKIE_NAME = "__Host-cloud_workbench_session"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class BrowserSessions:
    def __init__(self, store, settings):
        self.store = store
        self.origin = settings.get("dashboard_origin", "").rstrip("/")
        parsed = urlsplit(self.origin)
        dev_allowed = settings.get("dashboard_allow_http_loopback", False)
        loopback = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        if not parsed.hostname or "*" in parsed.hostname or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment:
            raise ValueError("dashboard_origin must be a configured exact origin")
        if parsed.scheme != "https" and not (parsed.scheme == "http" and loopback and dev_allowed):
            raise ValueError("dashboard requires HTTPS or explicit HTTP loopback development mode")
        parsed.port  # Validate port syntax before accepting the configured origin.
        self.secure = parsed.scheme == "https"
        self.cookie_name = COOKIE_NAME if self.secure else "cloud_workbench_session_dev"
        self.ttl = settings.get("dashboard_session_seconds", 1800)
        self.maximum = settings.get("dashboard_max_sessions", 128)
        if not 1 <= self.ttl <= 3600 or not 1 <= self.maximum <= 1024:
            raise ValueError("invalid dashboard session limits")
        self.sessions = {}
        self.lock = threading.RLock()
        self.csrf_key = secrets.token_bytes(32)
        self.login_attempts = deque()

    def check_host(self, request: Request) -> None:
        actual = f"{request.url.scheme}://{request.url.netloc}"
        if actual != self.origin:
            raise StoreError(403, "Dashboard origin does not match configured address")

    def check_origin(self, request: Request) -> None:
        self.check_host(request)
        if request.headers.get("origin") != self.origin:
            raise StoreError(403, "Same-origin request required")
        if request.headers.get("sec-fetch-site", "same-origin") != "same-origin":
            raise StoreError(403, "Cross-site request denied")

    def cleanup(self):
        clock = time.monotonic()
        self.sessions = {key: value for key, value in self.sessions.items() if value["expires"] > clock}
        while self.login_attempts and self.login_attempts[0] < clock - 60:
            self.login_attempts.popleft()

    def csrf(self, token: str) -> str:
        return hmac.new(self.csrf_key, token.encode(), hashlib.sha256).hexdigest()

    def record(self, request: Request):
        self.check_host(request)
        origin = request.headers.get("origin")
        if origin is not None and origin != self.origin:
            raise StoreError(403, "Cross-origin session access denied")
        token = request.cookies.get(self.cookie_name, "")
        if not token or len(token) > 256:
            raise StoreError(401, "Browser login required")
        with self.lock:
            self.cleanup()
            record = self.sessions.get(_digest(token))
        if record is None:
            raise StoreError(401, "Browser session expired; sign in again")
        # Refresh scopes and revocation every request; retain no reusable bearer.
        with self.store._connect() as db:
            row = db.execute("SELECT id,name,scopes,projects FROM clients WHERE id=? AND token_hash=? AND revoked_at IS NULL", (record["principal_id"], record["credential_hash"])).fetchone()
        if row is None:
            with self.lock:
                self.sessions.pop(_digest(token), None)
            raise StoreError(401, "Browser credential revoked")
        principal = {**dict(row), "scopes": json.loads(row["scopes"]), "projects": json.loads(row["projects"])}
        return token, record, principal


def _manager(request: Request) -> BrowserSessions:
    manager = getattr(request.app.state, "browser_sessions", None)
    if manager is None:
        raise StoreError(401, "Browser authentication unavailable")
    return manager


def validate_csrf(request: Request) -> None:
    """Validate a cookie-authenticated mutation; bearer-only clients bypass this."""
    manager = _manager(request)
    manager.check_origin(request)
    token, _, _ = manager.record(request)
    supplied = request.headers.get("x-csrf-token", "")
    if len(supplied) != 64 or not hmac.compare_digest(manager.csrf(token), supplied):
        raise StoreError(403, "CSRF validation failed")


def principal_from_cookie(request: Request) -> dict:
    manager = _manager(request)
    _, _, principal = manager.record(request)
    if request.method not in SAFE_METHODS:
        validate_csrf(request)
    return principal


def mount_dashboard(app, store, settings):
    """Mount once. Existing API principal dependency must explicitly call helper."""
    manager = BrowserSessions(store, settings)
    app.state.browser_sessions = manager
    template = (Path(__file__).parent / "static" / "dashboard.html").read_text()

    def response_headers():
        headers = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer", "Permissions-Policy": "camera=(), microphone=(), geolocation=()"}
        if manager.secure:
            headers["Strict-Transport-Security"] = "max-age=31536000"
        return headers

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def dashboard(request: Request):
        manager.check_host(request)
        nonce = secrets.token_urlsafe(24)
        headers = response_headers()
        headers["Content-Security-Policy"] = f"default-src 'none'; script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'; connect-src 'self'; img-src 'self' data:; font-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'; object-src 'none'"
        return HTMLResponse(template.replace("__CSP_NONCE__", nonce), headers=headers)

    @app.post("/auth/session", include_in_schema=False)
    def login(request: Request):
        manager.check_origin(request)
        authorization = request.headers.get("authorization", "")
        if not authorization.startswith("Bearer ") or len(authorization) > 1024:
            raise StoreError(401, "Bearer credential required for sign in")
        bearer = authorization[7:]
        principal = store.authenticate(bearer)
        if principal is None:
            # Failed attempts cannot consume a valid owner's sign-in allowance.
            with manager.lock:
                manager.cleanup()
                if len(manager.login_attempts) >= 20:
                    raise StoreError(429, "Sign-in rate exceeded; try again in a minute")
                manager.login_attempts.append(time.monotonic())
            raise StoreError(401, "Invalid sign-in credential")
        if "observe" not in principal["scopes"]:
            raise StoreError(403, "Dashboard requires observe scope")
        token = secrets.token_urlsafe(32)
        with manager.lock:
            manager.cleanup()
            old = request.cookies.get(manager.cookie_name)
            if old:
                manager.sessions.pop(_digest(old), None)
            if len(manager.sessions) >= manager.maximum:
                raise StoreError(503, "Browser session limit reached")
            manager.sessions[_digest(token)] = {"principal_id": principal["id"], "credential_hash": _digest(bearer), "expires": time.monotonic() + manager.ttl}
        result = JSONResponse({"name": principal["name"], "csrf": manager.csrf(token), "expires_in": manager.ttl}, headers=response_headers())
        result.set_cookie(manager.cookie_name, token, max_age=manager.ttl, httponly=True, secure=manager.secure, samesite="strict", path="/")
        return result

    @app.get("/auth/session", include_in_schema=False)
    def session(request: Request):
        token, record, principal = manager.record(request)
        return JSONResponse({"name": principal["name"], "csrf": manager.csrf(token), "expires_in": max(0, int(record["expires"] - time.monotonic()))}, headers=response_headers())

    @app.delete("/auth/session", include_in_schema=False)
    def logout(request: Request):
        validate_csrf(request)
        with manager.lock:
            manager.sessions.pop(_digest(request.cookies.get(manager.cookie_name, "")), None)
        result = JSONResponse({"signed_out": True}, headers=response_headers())
        result.delete_cookie(manager.cookie_name, path="/", secure=manager.secure, httponly=True, samesite="strict")
        return result

    @app.get("/auth/config", include_in_schema=False)
    def configuration(request: Request):
        principal = principal_from_cookie(request)
        projects = []
        for project_id, config in settings.get("projects", {}).items():
            if project_id not in principal["projects"]:
                continue
            agents = [agent for agent in config.get("allowed_agents", []) if agent != "fixture" or settings.get("test_mode", False)]
            versions = config.get("environment_versions", [])
            default = versions[0] if versions else None
            environments = {agent: {"versions": list(versions), "default": default} for agent in agents}
            if settings.get("environment_registry"):
                default = None
                environments = {agent: {"versions": [], "default": None} for agent in agents}
                try:
                    registry = EnvironmentRegistry(settings['environment_registry'], read_only=True)
                    active = registry.active(project_id)
                    version = active['manifest']['version'] if active else None
                    if version in versions:
                        default = version
                    for version in versions:
                        try:
                            record = registry.resolve(project_id, version,
                                operator_approved_sha256=config.get('operator_approved_environments', {}).get(version))
                            manifest = record['manifest']
                            for agent in agents:
                                if agent in manifest['cli_versions']:
                                    environments[agent]['versions'].append(version)
                        except (OSError, ValueError, sqlite3.Error):
                            continue
                    for choices in environments.values():
                        compatible = choices['versions']
                        choices['default'] = default if default in compatible else compatible[0] if len(compatible) == 1 else None
                except (OSError, ValueError, sqlite3.Error):
                    pass
            projects.append({"id": project_id, "name": config.get("display_name", project_id), "agents": list(agents), "models": config.get("models", {}), "environment_versions": versions, "default_environment_version": default, "environments_by_agent": environments})
        return JSONResponse({"projects": projects, "scopes": principal["scopes"], "test_mode": bool(settings.get("test_mode", False)), "release_status": settings.get("release_status", "Pre-release; release gates unverified"), "capabilities": settings.get("capabilities", {}), "input_max_bytes": min(settings.get("input_max_bytes", 104857600), store.policy["input_max_bytes"], 16777216)}, headers=response_headers())

    return manager
