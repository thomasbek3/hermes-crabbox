from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading

import pytest

from cloudworkbench.cli import Client, ClientError, main


@pytest.fixture
def token_file(tmp_path):
    path = tmp_path / "token"
    path.write_text("SECRET_TEST_TOKEN")
    path.chmod(0o600)
    return path


@contextmanager
def server(response_body=b"{}", status=200, content_type="application/json", extra_headers=None):
    calls = []
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self): self.respond()
        def do_POST(self): self.respond()
        def do_DELETE(self): self.respond()
        def respond(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            calls.append((self.command, self.path, dict(self.headers), body))
            selected_body = response_body(self.path) if callable(response_body) else response_body
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(selected_body)))
            for name, value in (extra_headers or {}).items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(selected_body)
        def log_message(self, *args): pass
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_port}", calls
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join()


def args(server_url, token_file, *rest):
    return ["--server", server_url, "--token-file", str(token_file), *rest]


def test_submit_preserves_literal_prompt_and_idempotency(token_file, capsys):
    with server(b'{"state":"queued"}') as (url, calls):
        assert main(args(url, token_file, "submit", "literal $(printf hi)", "--project", "project", "--idempotency-key", "retry-key")) == 0
    method, path, headers, body = calls[0]
    assert (method, path) == ("POST", "/v1/sessions")
    assert headers["Idempotency-Key"] == "retry-key"
    assert json.loads(body)["goal"] == "literal $(printf hi)"
    assert "SECRET_TEST_TOKEN" not in capsys.readouterr().out


@pytest.mark.parametrize("command,expected", [(["message", "abc", "next"], "/v1/sessions/abc/messages"), (["resume", "abc"], "/v1/sessions/abc/resume"), (["cancel", "attempt"], "/v1/attempts/attempt/cancel"), (["archive", "abc"], "/v1/sessions/abc/archive")])
def test_mutation_routes(token_file, command, expected):
    with server() as (url, calls):
        assert main(args(url, token_file, *command)) == 0
    assert calls[0][1] == expected
    assert calls[0][2]["Idempotency-Key"]


def test_http_failure_does_not_print_secret(token_file, capsys):
    with server(b'{"detail":"SECRET_TEST_TOKEN failed"}', 403) as (url, _):
        assert main(args(url, token_file, "list")) == 1
    output = capsys.readouterr()
    assert "HTTP 403" in output.err
    assert "SECRET_TEST_TOKEN" not in output.err + output.out


def test_job_failure_returns_nonzero(token_file):
    with server(b'{"state":"completed","outcome":"rejected"}') as (url, _):
        assert main(args(url, token_file, "show", "abc")) == 1


def test_binary_download_no_clobber(token_file, tmp_path):
    blob = bytes(range(256)) * 100
    destination = tmp_path / "download.bin"
    with server(blob, content_type="application/octet-stream") as (url, _):
        assert main(args(url, token_file, "download", "artifact", str(destination))) == 0
        assert destination.read_bytes() == blob
        assert main(args(url, token_file, "download", "artifact", str(destination))) == 1
        assert destination.read_bytes() == blob
    assert not list(tmp_path.glob(".cloud2-download-*"))


def test_redirect_does_not_forward_token(token_file):
    with server(extra_headers={"Location": "http://127.0.0.1:1/stolen"}, status=302) as (url, calls):
        assert main(args(url, token_file, "list")) == 1
    assert len(calls) == 1


def test_token_permissions_and_symlink_rejected(token_file, tmp_path):
    token_file.chmod(0o644)
    with pytest.raises(ClientError):
        Client("http://localhost:7788", token_file)
    token_file.chmod(0o600)
    link = tmp_path / "link"
    link.symlink_to(token_file)
    with pytest.raises(OSError):
        Client("http://localhost:7788", link)


def test_follow_terminal_sse(token_file, capsys):
    events = b'id: 10\ndata: {"payload":{"state":"running"}}\n\nid: 11\ndata: {"payload":{"state":"completed","outcome":"verified"}}\n\n'
    with server(lambda path: events if "/events?" in path else b'{"state":"completed","outcome":"verified"}', content_type="text/event-stream") as (url, calls):
        assert main(args(url, token_file, "follow", "abc", "--after", "9")) == 0
    assert "after=9" in calls[0][1]
    assert "completed" in capsys.readouterr().out


def test_unknown_event_stream_ending_is_nonzero(token_file):
    with server(b'data: {"payload":{"state":"unknown"}}\n\n', content_type="text/event-stream") as (url, _):
        assert main(args(url, token_file, "follow", "abc")) == 1


def test_delete_requires_confirmation_without_network(token_file):
    assert main(args("http://127.0.0.1:1", token_file, "delete", "abc")) == 1


def test_follow_does_not_stop_at_old_completed_attempt(token_file, capsys):
    events = b'id: 1\ndata: {"payload":{"state":"completed","outcome":"verified"}}\n\nid: 2\ndata: {"payload":{"state":"running"}}\n\nid: 3\ndata: {"payload":{"state":"completed","outcome":"rejected"}}\n\n'
    snapshots = iter([b'{"state":"running"}', b'{"state":"completed","outcome":"rejected"}'])
    with server(lambda path: events if "/events?" in path else next(snapshots), content_type="text/event-stream") as (url, _):
        assert main(args(url, token_file, "follow", "abc")) == 1
    assert "rejected" in capsys.readouterr().out


def test_digest_mismatch_never_publishes_download(token_file, tmp_path):
    destination = tmp_path / "file"
    with server(b"wrong content", content_type="application/octet-stream", extra_headers={"ETag": '"' + "0" * 64 + '"'}) as (url, _):
        assert main(args(url, token_file, "download", "artifact", str(destination))) == 1
    assert not destination.exists()
    assert not list(tmp_path.glob(".cloud2-download-*"))


def test_follow_replays_to_latest_completed_attempt(token_file, capsys):
    events = b'id: 1\ndata: {"attempt_id":"old","payload":{"state":"completed","outcome":"verified"}}\n\nid: 2\ndata: {"attempt_id":"latest","payload":{"state":"completed","outcome":"verified"}}\n\n'
    snapshot = b'{"state":"completed","outcome":"verified","attempts":[{"id":"old"},{"id":"latest"}]}'
    with server(lambda path: events if "/events?" in path else snapshot, content_type="text/event-stream") as (url, _):
        assert main(args(url, token_file, "follow", "abc")) == 0
    assert "latest" in capsys.readouterr().out


def test_bundle_download_route_digest_and_no_clobber(token_file, tmp_path):
    import hashlib
    blob = b'PK\x03\x04test ZIP transfer bytes'
    destination = tmp_path / 'result.zip'
    with server(blob, content_type='application/zip', extra_headers={'ETag': '"' + hashlib.sha256(blob).hexdigest() + '"'}) as (url, calls):
        assert main(args(url, token_file, 'bundle', 'session-id', str(destination))) == 0
        assert calls[0][1] == '/v1/sessions/session-id/bundle'
        assert destination.read_bytes() == blob
        assert main(args(url, token_file, 'bundle', 'session-id', str(destination))) == 1
        assert destination.read_bytes() == blob
    assert not list(tmp_path.glob('.cloud2-download-*'))


@pytest.mark.parametrize('mime,digest', [('text/html', '0'*64), ('application/zip', None), ('application/zip', 'invalid'), ('application/zip', '0'*64)])
def test_bundle_invalid_protocol_or_digest_never_publishes(token_file, tmp_path, mime, digest):
    destination = tmp_path / 'refused.zip'
    extra = {'ETag': '"'+digest+'"'} if digest else {}
    with server(b'wrong data', content_type=mime, extra_headers=extra) as (url, _):
        assert main(args(url, token_file, 'bundle', 'session-id', str(destination))) == 1
    assert not destination.exists()
    assert not list(tmp_path.glob('.cloud2-download-*'))


@pytest.mark.parametrize('length', ['invalid', '\u00b2', '-1', '9'*100, '9999999999'])
@pytest.mark.parametrize('bundle', [False, True])
def test_download_invalid_content_length_is_controlled(token_file, tmp_path, length, bundle):
    import io
    import hashlib
    class Response(io.BytesIO):
        headers = {'Content-Length': length, 'Content-Type': 'application/zip', 'ETag': '"'+hashlib.sha256(b'x').hexdigest()+'"'}
    client = Client('http://127.0.0.1:1', token_file)
    client.request = lambda *args: Response(b'x')
    destination = tmp_path/'never-publish'
    with pytest.raises(ClientError, match='Content-Length'):
        client._download('/fixture', destination, bundle=bundle)
    assert not destination.exists() and not list(tmp_path.glob('.cloud2-download-*'))


def test_bundle_weak_etag_and_encoded_body_refused(token_file, tmp_path):
    import hashlib
    for extra in ({'ETag': 'W/"'+hashlib.sha256(b'x').hexdigest()+'"'},
                  {'ETag': '"'+hashlib.sha256(b'x').hexdigest()+'"', 'Content-Encoding': 'gzip'}):
        with server(b'x', content_type='application/zip', extra_headers=extra) as (url, _):
            assert main(args(url, token_file, 'bundle', 'session-id', str(tmp_path/'refused'))) == 1
    assert not (tmp_path/'refused').exists()
