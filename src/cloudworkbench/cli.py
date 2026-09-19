"""Small authenticated client for the cloud workbench API."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from pathlib import Path
import stat
import sys
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
import uuid


class ClientError(Exception):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ClientError("server redirect refused")


class Client:
    def __init__(self, server: str, token_file: Path, timeout: float = 30):
        parsed = urlsplit(server)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path.rstrip("/"):
            raise ClientError("server must be an HTTP(S) origin without credentials")
        if timeout <= 0 or timeout > 300:
            raise ClientError("timeout must be between 0 and 300 seconds")
        fd = os.open(token_file, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_size > 16384:
                raise ClientError("token file must be private, owner-owned, and regular (chmod 600)")
            self.token = os.read(fd, 16385).decode().strip()
        finally:
            os.close(fd)
        if not self.token or any(ord(char) < 33 or ord(char) > 126 for char in self.token):
            raise ClientError("invalid token file")
        self.server = server.rstrip("/")
        self.timeout = timeout
        self.opener = build_opener(NoRedirect())

    def clean(self, text: str) -> str:
        return text.replace(self.token, "[REDACTED]")

    def request(self, method: str, path: str, data=None, key: str | None = None):
        headers = {"Authorization": "Bearer " + self.token, "Accept": "application/json"}
        payload = None
        if data is not None:
            payload = json.dumps(data).encode()
            headers["Content-Type"] = "application/json"
        if key:
            headers["Idempotency-Key"] = key
        request = Request(self.server + "/v1" + path, data=payload, headers=headers, method=method)
        try:
            return self.opener.open(request, timeout=self.timeout)
        except HTTPError as exc:
            detail = exc.read(4096).decode("utf-8", errors="replace")
            raise ClientError(self.clean(f"HTTP {exc.code}: {detail}")) from None
        except (URLError, TimeoutError) as exc:
            raise ClientError(self.clean(f"connection failed: {exc}")) from None

    def json(self, method: str, path: str, data=None, key=None):
        with self.request(method, path, data, key) as response:
            content = response.read(8 * 1024**2 + 1)
            if len(content) > 8 * 1024**2:
                raise ClientError("JSON response exceeds client limit")
            try:
                return json.loads(content)
            except (ValueError, UnicodeError):
                raise ClientError("server returned invalid JSON") from None

    def follow(self, session_id: str, after: int, max_seconds: float) -> int:
        deadline = time.monotonic() + max_seconds
        last_id = after
        path = f"/sessions/{segment(session_id)}/events?" + urlencode({"after": after, "follow": "true"})
        with self.request("GET", path) as response:
            if not response.headers.get("Content-Type", "").startswith("text/event-stream"):
                raise ClientError("server did not return an event stream")
            data = []
            length = 0
            event_id = None
            while time.monotonic() < deadline:
                raw = response.readline(1024 * 1024 + 1)
                if len(raw) > 1024 * 1024:
                    raise ClientError("event line exceeds client limit")
                if not raw:
                    snapshot = self.json("GET", f"/sessions/{segment(session_id)}")
                    state, outcome = outcome_of(snapshot)
                    if state in ("completed", "failed", "cancelled", "interrupted", "paused"):
                        return int(state in ("failed", "cancelled", "interrupted") or outcome == "rejected")
                    raise ClientError(f"event stream ended; reconnect with --after {last_id}")
                line = raw.decode("utf-8", errors="strict").rstrip("\r\n")
                if not line:
                    if data:
                        try:
                            event = json.loads("\n".join(data))
                        except ValueError:
                            raise ClientError("server returned invalid event JSON") from None
                        if event_id is not None:
                            last_id = event_id
                        print(self.clean(json.dumps(event)), flush=True)
                        state, outcome = outcome_of(event)
                        if state in ("completed", "failed", "cancelled", "interrupted", "paused"):
                            # Historical attempts may finish while a newer turn is queued.
                            snapshot = self.json("GET", f"/sessions/{segment(session_id)}")
                            current_state, current_outcome = outcome_of(snapshot)
                            attempts = snapshot.get("attempts", []) if isinstance(snapshot, dict) else []
                            latest_id = attempts[-1].get("id") if attempts else None
                            is_latest = latest_id is None or event.get("attempt_id") == latest_id
                            if is_latest and current_state in ("completed", "failed", "cancelled", "interrupted", "paused"):
                                return int(current_state in ("failed", "cancelled", "interrupted") or current_outcome == "rejected")
                    data, length, event_id = [], 0, None
                elif line.startswith("data:"):
                    value = line[5:].lstrip(" ")
                    length += len(value)
                    if length > 1024 * 1024:
                        raise ClientError("event exceeds client limit")
                    data.append(value)
                elif line.startswith("id:"):
                    try:
                        event_id = int(line[3:].strip())
                    except ValueError:
                        raise ClientError("invalid event sequence") from None
            raise ClientError(f"follow duration reached; reconnect with --after {last_id}")

    def download(self, artifact_id: str, destination: Path) -> None:
        self._download(f"/artifacts/{segment(artifact_id)}/content", destination)

    def bundle(self, session_id: str, destination: Path) -> None:
        self._download(f"/sessions/{segment(session_id)}/bundle", destination, bundle=True)

    def _download(self, path: str, destination: Path, *, bundle=False) -> None:
        destination = Path(os.path.abspath(destination))
        fd, staging = tempfile.mkstemp(prefix=".cloud2-download-", dir=destination.parent)
        try:
            with os.fdopen(fd, "wb") as output:
                with self.request("GET", path) as response:
                    total = 0
                    digest = hashlib.sha256()
                    expected_digest = response.headers.get("ETag", "").strip('"')
                    expected = response.headers.get("Content-Length")
                    limit = 16 * 1024**2 if bundle else 2 * 1024**3
                    if expected is not None and (not re.fullmatch(r'[0-9]{1,20}', expected) or int(expected) > limit):
                        raise ClientError("download Content-Length is invalid or exceeds client limit")
                    if bundle:
                        if response.headers.get("Content-Encoding", "identity") != "identity":
                            raise ClientError("encoded bundle responses are not supported")
                        if response.headers.get("Content-Type", "").split(";", 1)[0].strip() != "application/zip":
                            raise ClientError("server did not return a ZIP bundle")
                        if not re.fullmatch(r'[0-9a-f]{64}', expected_digest):
                            raise ClientError("bundle SHA256 ETag is missing or invalid")
                        if expected is None or not 0 < int(expected) <= limit:
                            raise ClientError("bundle Content-Length is missing or exceeds client limit")
                    while chunk := response.read(128 * 1024):
                        total += len(chunk)
                        if total > limit:
                            raise ClientError("download exceeds client byte limit")
                        digest.update(chunk)
                        output.write(chunk)
                    if len(expected_digest) == 64 and digest.hexdigest() != expected_digest:
                        raise ClientError("download digest mismatch")
                    if expected is not None and total != int(expected):
                        raise ClientError("incomplete download")
                output.flush()
                os.fsync(output.fileno())
            # Hard-link publication fails if destination exists, including a symlink.
            os.link(staging, destination)
        finally:
            os.unlink(staging)


def segment(value: str) -> str:
    if not value or value in (".", "..") or "/" in value or "\\" in value:
        raise ClientError("invalid resource identifier")
    return quote(value, safe="")


def outcome_of(value) -> tuple[str | None, str | None]:
    if not isinstance(value, dict):
        return None, None
    state, outcome = value.get("state"), value.get("outcome")
    for name in ("payload", "attempt", "result"):
        if isinstance(value.get(name), dict):
            nested_state, nested_outcome = outcome_of(value[name])
            state, outcome = state or nested_state, outcome or nested_outcome
    return state, outcome


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="Private cloud agent workbench")
    root.add_argument("--server", default=os.environ.get("CLOUD2_SERVER", "http://127.0.0.1:7788"))
    root.add_argument("--token-file", type=Path, default=Path(os.environ.get("CLOUD2_TOKEN_FILE", "~/.config/cloud2/token")).expanduser())
    root.add_argument("--timeout", type=float, default=30)
    commands = root.add_subparsers(dest="command", required=True)
    submit = commands.add_parser("submit")
    submit.add_argument("goal")
    submit.add_argument("--project", required=True)
    submit.add_argument("--agent", default="claude")
    submit.add_argument("--model")
    submit.add_argument("--environment-version")
    submit.add_argument("--acceptance", action="append", default=[])
    submit.add_argument("--input-id", action="append", default=[])
    listing = commands.add_parser("list")
    listing.add_argument("--limit", type=int, default=50)
    listing.add_argument("--offset", type=int, default=0)
    for name in ("show", "message", "resume", "artifacts", "archive", "delete", "follow"):
        command = commands.add_parser(name)
        command.add_argument("session_id")
        if name == "message":
            command.add_argument("message")
        if name == "delete":
            command.add_argument("--confirm", action="store_true", help="explicitly request irreversible purge")
        if name == "follow":
            command.add_argument("--after", type=int, default=0)
            command.add_argument("--max-seconds", type=float, default=3600)
        if name in ("message", "resume", "archive", "delete"):
            command.add_argument("--idempotency-key")
    submit.add_argument("--idempotency-key")
    cancel = commands.add_parser("cancel")
    cancel.add_argument("attempt_id")
    cancel.add_argument("--idempotency-key")
    download = commands.add_parser("download")
    download.add_argument("artifact_id")
    download.add_argument("destination", type=Path)
    bundle = commands.add_parser("bundle", help="download the latest terminal attempt result ZIP")
    bundle.add_argument("session_id")
    bundle.add_argument("destination", type=Path)
    return root


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    client = None
    try:
        if args.command == "delete" and not args.confirm:
            raise ClientError("delete requires --confirm; use cancel to stop work or archive to hide it")
        if args.command == "follow" and (args.after < 0 or not 0 < args.max_seconds <= 86400):
            raise ClientError("follow requires nonnegative --after and --max-seconds between 0 and 86400")
        client = Client(args.server, args.token_file, args.timeout)
        command = args.command
        key = getattr(args, "idempotency_key", None) or str(uuid.uuid4())
        if command == "submit":
            payload = {"project_id": args.project, "goal": args.goal, "agent": args.agent, "acceptance": args.acceptance, "input_ids": args.input_id}
            for name in ("model", "environment_version"):
                if getattr(args, name):
                    payload[name] = getattr(args, name)
            result = client.json("POST", "/sessions", payload, key)
        elif command == "list":
            result = client.json("GET", "/sessions?" + urlencode({"limit": args.limit, "offset": args.offset}))
        elif command == "cancel":
            result = client.json("POST", f"/attempts/{segment(args.attempt_id)}/cancel", {}, key)
        elif command == "download":
            client.download(args.artifact_id, args.destination)
            return 0
        elif command == "bundle":
            client.bundle(args.session_id, args.destination)
            return 0
        elif command == "follow":
            return client.follow(args.session_id, args.after, args.max_seconds)
        else:
            base = f"/sessions/{segment(args.session_id)}"
            if command == "show":
                result = client.json("GET", base)
            elif command == "artifacts":
                result = client.json("GET", base + "/artifacts")
            elif command == "delete":
                result = client.json("DELETE", base, {}, key)
            else:
                suffix = "messages" if command == "message" else command
                result = client.json("POST", base + "/" + suffix, {"message": args.message} if command == "message" else {}, key)
        print(client.clean(json.dumps(result, indent=2)))
        state, outcome = outcome_of(result)
        return int(state in ("failed", "cancelled", "interrupted") or outcome == "rejected")
    except (ClientError, OSError, ValueError) as exc:
        message = str(exc)
        print("cloud2: " + (client.clean(message) if client else message), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("cloud2: disconnected; remote work continues", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
