"""Minimal public-address-only HTTPS CONNECT gateway; no arbitrary HTTP forwarding."""
from __future__ import annotations

import argparse
import ipaddress
import re
import select
import socket
import socketserver
import threading
import time

HOST = re.compile(r"(?=.{1,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
MAX_HEADER = 8192
TUNNEL_IDLE_SECONDS = 600
TUNNEL_LIFETIME_SECONDS = 3600


class Denied(ValueError):
    pass


def hostname(value: str) -> str:
    value = value.lower()
    if not HOST.fullmatch(value) or value.endswith(".local"):
        raise Denied("invalid destination hostname")
    return value


def parse_connect(header: bytes, allowlist: set[str]) -> tuple[str, int]:
    if len(header) > MAX_HEADER or not header.endswith(b"\r\n\r\n") or b"\x00" in header:
        raise Denied("invalid request framing")
    try:
        lines = header.decode("ascii").split("\r\n")
        method, authority, version = lines[0].split(" ")
        host, port = authority.rsplit(":", 1)
    except (UnicodeError, ValueError) as exc:
        raise Denied("invalid CONNECT request") from exc
    if method != "CONNECT" or version not in ("HTTP/1.0", "HTTP/1.1") or port != "443":
        raise Denied("only CONNECT on port 443 is permitted")
    host = hostname(host)
    if host not in allowlist:
        raise Denied("destination is not allowlisted")
    if len(lines) > 35:
        raise Denied("too many headers")
    seen = set()
    for line in lines[1:-2]:
        if ":" not in line or line[:1].isspace():
            raise Denied("invalid header")
        key, value = line.split(":", 1)
        key = key.lower()
        if key in seen or not re.fullmatch(r"[a-z0-9-]+", key) or any(ord(c) < 32 and c != "\t" for c in value):
            raise Denied("ambiguous header")
        seen.add(key)
        if key in ("content-length", "transfer-encoding", "upgrade"):
            raise Denied("request bodies are forbidden")
        if key == "host" and value.strip().lower() != authority.lower():
            raise Denied("mismatched Host header")
    return host, 443


def public_addresses(host: str, port: int = 443) -> list[tuple]:
    records = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    addresses = []
    for family, socktype, proto, _canonname, sockaddr in records:
        address = ipaddress.ip_address(sockaddr[0])
        # Reject IPv4-mapped and transition addresses too, even if is_global varies.
        if not address.is_global or address.is_multicast or address.is_reserved or (isinstance(address, ipaddress.IPv6Address) and (address.ipv4_mapped or address.sixtofour or address.teredo)):
            raise Denied("DNS returned non-public or transition address")
        entry = (family, socktype, proto, sockaddr)
        if entry not in addresses:
            addresses.append(entry)
    if not addresses or len(addresses) > 64:
        raise Denied("invalid DNS answer count")
    return addresses


def connect_public(host: str, port: int = 443) -> socket.socket:
    # Validate every returned address, then connect numeric sockaddr directly:
    # a second DNS resolution could otherwise rebind into a private network.
    for family, socktype, proto, sockaddr in public_addresses(host, port):
        stream = socket.socket(family, socktype, proto)
        stream.settimeout(10)
        try:
            stream.connect(sockaddr)
            return stream
        except OSError:
            stream.close()
    raise Denied("destination unavailable")


class Gateway(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True
    request_queue_size = 16

    def __init__(self, address, allowed: set[str]):
        self.allowed = {hostname(h) for h in allowed}
        if not self.allowed:
            raise Denied("empty egress allowlist")
        self.slots = threading.BoundedSemaphore(32)
        super().__init__(address, Handler)

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            request.close()
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()

    def handle_error(self, request, client_address):
        # Do not log URLs, headers, TLS payload, credentials or caller input.
        pass


class Handler(socketserver.BaseRequestHandler):
    def handle(self):
        upstream = None
        established = False
        try:
            self.request.settimeout(10)
            header = bytearray()
            header_deadline = time.monotonic() + 10
            # One byte avoids accidentally consuming unbounded pipelined TLS data.
            while not header.endswith(b"\r\n\r\n"):
                if time.monotonic() >= header_deadline:
                    raise Denied("header timeout")
                self.request.settimeout(max(0.001, header_deadline-time.monotonic()))
                if len(header) >= MAX_HEADER:
                    raise Denied("header too large")
                part = self.request.recv(1)
                if not part:
                    return
                header.extend(part)
            host, port = parse_connect(bytes(header), self.server.allowed)
            upstream = connect_public(host, port)
            self.request.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            established = True
            deadline = time.monotonic() + TUNNEL_LIFETIME_SECONDS
            remaining = 128 * 1024 * 1024
            streams = (self.request, upstream)
            for stream in streams:
                stream.settimeout(10)
            while remaining > 0 and time.monotonic() < deadline:
                ready, _, _ = select.select(streams, [], [], min(TUNNEL_IDLE_SECONDS, deadline-time.monotonic()))
                if not ready:
                    return
                for source in ready:
                    data = source.recv(min(65536, remaining))
                    if not data:
                        return
                    remaining -= len(data)
                    target = upstream if source is self.request else self.request
                    target.sendall(data)
        except (Denied, OSError, ValueError):
            if not established:
                try:
                    self.request.sendall(b"HTTP/1.1 403 Forbidden\r\nConnection: close\r\nContent-Length: 0\r\n\r\n")
                except OSError:
                    pass
        finally:
            if upstream:
                upstream.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--listen", required=True)
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--allow", action="append", required=True)
    args = parser.parse_args()
    Gateway((args.listen, args.port), set(args.allow)).serve_forever(poll_interval=0.5)


if __name__ == "__main__":
    main()
