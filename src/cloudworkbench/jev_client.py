"""Server-side TypeSafe transport; policy and Choice semantics belong to the selector."""
from __future__ import annotations

import http.client
import json
import math
import ssl
import time

ENDPOINT = "https://api.typesafe.ai/v1/systemone"
MAX_REQUEST_BYTES = 256 * 1024
MAX_RESPONSE_BYTES = 1024 * 1024
_CODES = frozenset({
    "invalid_api_key", "invalid_timeout", "invalid_request", "request_too_large",
    "timeout", "tls_error", "network_error", "redirect_refused", "unauthorized",
    "request_rejected", "rate_limited", "unavailable", "http_error",
    "response_too_large", "invalid_response",
})


class JevError(Exception):
    """Only fixed codes and elapsed duration escape the transport boundary."""

    def __init__(self, code: str, *, elapsed_seconds: float | None = None):
        self.code = code if code in _CODES else "network_error"
        self.elapsed_seconds = elapsed_seconds
        super().__init__(self.code)


def _json_shape(value):
    pending = [(value, 0)]
    count = 0
    while pending:
        item, depth = pending.pop()
        count += 1
        if depth > 64 or count > 20_000:
            raise ValueError("json_complexity")
        if type(item) is dict:
            if any(type(key) is not str for key in item):
                raise ValueError("json_key")
            if len(item) + len(pending) > 20_000:
                raise ValueError("json_complexity")
            pending.extend((child, depth + 1) for child in item.values())
        elif type(item) is list:
            if len(item) + len(pending) > 20_000:
                raise ValueError("json_complexity")
            pending.extend((child, depth + 1) for child in item)
        elif type(item) is float:
            if not math.isfinite(item):
                raise ValueError("json_nonfinite")
        elif item is not None and type(item) not in (str, int, bool):
            raise ValueError("json_type")


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("json_duplicate")
        result[key] = value
    return result


def _constant(_):
    raise ValueError("json_nonfinite")


class JevClient:
    """One direct TLS request per evaluate call, without proxy, redirect or retry.

    Socket timeouts use the remaining monotonic budget. DNS resolution and OS I/O
    are not forcibly interrupted, so timeout_seconds is not a hard wall deadline.
    last_elapsed_seconds is observational last-call state; use one client per task.
    """

    def __init__(self, api_key: str, timeout_seconds=10):
        if (type(api_key) is not str or not 1 <= len(api_key) <= 4096
                or any(not 33 <= ord(char) <= 126 for char in api_key)):
            raise JevError("invalid_api_key")
        if (type(timeout_seconds) not in (int, float)
                or not 0 < timeout_seconds <= 60):
            raise JevError("invalid_timeout")
        self._api_key = api_key
        self.timeout_seconds = float(timeout_seconds)
        self.last_elapsed_seconds = None
        self.last_error_code = None

    def evaluate(self, payload: dict) -> dict:
        start = time.monotonic()
        self.last_error_code = None
        connection = None
        response = None
        error = None
        result = None
        try:
            if type(payload) is not dict:
                raise JevError("invalid_request")
            try:
                _json_shape(payload)
                body = bytearray()
                for chunk in json.JSONEncoder(ensure_ascii=False, allow_nan=False,
                                              separators=(",", ":")).iterencode(payload):
                    encoded = chunk.encode("utf-8")
                    if len(body) + len(encoded) > MAX_REQUEST_BYTES:
                        raise JevError("request_too_large")
                    body.extend(encoded)
            except (ValueError, TypeError, OverflowError, RecursionError):
                raise JevError("invalid_request") from None

            def remaining():
                seconds = self.timeout_seconds - (time.monotonic() - start)
                if seconds <= 0:
                    raise JevError("timeout")
                return seconds

            connection = http.client.HTTPSConnection("api.typesafe.ai", port=443,
                timeout=remaining(), context=ssl.create_default_context())
            connection.connect()
            sock = connection.sock
            sock.settimeout(remaining())
            connection.request("POST", "/v1/systemone", body=bytes(body), headers={
                "Authorization": "Bearer " + self._api_key,
                "Content-Type": "application/json", "Accept": "application/json",
                "Accept-Encoding": "identity", "Connection": "close",
            })
            sock.settimeout(remaining())
            response = connection.getresponse()
            remaining()
            status = response.status
            if status != 200:
                code = ("redirect_refused" if 300 <= status < 400 else
                        "unauthorized" if status in (401, 403) else
                        "request_rejected" if status in (400, 422) else
                        "rate_limited" if status == 429 else
                        "unavailable" if 500 <= status < 600 else "http_error")
                raise JevError(code)
            if (response.getheader("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json"
                    or response.getheader("Content-Encoding", "identity").lower() != "identity"):
                raise JevError("invalid_response")
            length = response.getheader("Content-Length")
            if length is not None:
                if not length.isascii() or not length.isdecimal() or len(length) > 12:
                    raise JevError("invalid_response")
                length = int(length)
                if length > MAX_RESPONSE_BYTES:
                    raise JevError("response_too_large")
            data = bytearray()
            # A complete Content-Length read can close the response and its
            # last socket reference; do not touch that socket again at EOF.
            while not response.isclosed():
                sock.settimeout(remaining())
                chunk = response.read1(min(65536, MAX_RESPONSE_BYTES + 1 - len(data)))
                remaining()
                if not chunk:
                    break
                data.extend(chunk)
                if len(data) > MAX_RESPONSE_BYTES:
                    raise JevError("response_too_large")
            if length is not None and len(data) != length:
                raise JevError("invalid_response")
            try:
                result = json.loads(data.decode("utf-8"), object_pairs_hook=_pairs,
                                    parse_constant=_constant)
                if type(result) is not dict:
                    raise ValueError("json_object_required")
                _json_shape(result)
            except (ValueError, TypeError, OverflowError, RecursionError):
                raise JevError("invalid_response") from None
            remaining()
        except JevError as exc:
            error = exc.code
        except TimeoutError:
            error = "timeout"
        except ssl.SSLError:
            error = "tls_error"
        except (OSError, http.client.HTTPException):
            error = "network_error"
        finally:
            for resource in (response, connection):
                if resource is not None:
                    try:
                        resource.close()
                    except Exception:
                        error = error or "network_error"
            self.last_elapsed_seconds = max(0.0, time.monotonic() - start)
            self.last_error_code = error
        if error is not None:
            # Raise outside the except handler so underlying exceptions cannot
            # expose provider bodies, URLs, headers or credentials in tracebacks.
            raise JevError(error, elapsed_seconds=self.last_elapsed_seconds) from None
        return result
