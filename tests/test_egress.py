import socket
import threading
import pytest
from cloudworkbench import egress

ALLOWED={'api.anthropic.com','example.com'}

def test_valid_connect():
    assert egress.parse_connect(b'CONNECT api.anthropic.com:443 HTTP/1.1\r\nHost: api.anthropic.com:443\r\n\r\n',ALLOWED)==('api.anthropic.com',443)

@pytest.mark.parametrize('packet',[
 b'GET https://example.com HTTP/1.1\r\n\r\n',
 b'CONNECT example.com:80 HTTP/1.1\r\n\r\n',
 b'CONNECT 127.0.0.1:443 HTTP/1.1\r\n\r\n',
 b'CONNECT [::1]:443 HTTP/1.1\r\n\r\n',
 b'CONNECT evil.example.com:443 HTTP/1.1\r\n\r\n',
 b'CONNECT example.com.:443 HTTP/1.1\r\n\r\n',
 b'CONNECT example.com:443 HTTP/1.1\r\nHost: evil.com:443\r\n\r\n',
 b'CONNECT example.com:443 HTTP/1.1\r\nContent-Length: 4\r\n\r\n',
 b'CONNECT example.com:443 HTTP/1.1\r\nX-Foo: a\r\nX-Foo: b\r\n\r\n',
 b'CONNECT example.com:443 HTTP/1.1\r\n folded: x\r\n\r\n',
 b'CONNECT example.com:443 HTTP/1.1\n\n',
 b'CONNECT example.com:443 HTTP/1.1\r\nX-Foo: '+b'x'*8192+b'\r\n\r\n',
])
def test_request_parser_rejects_ambiguous_or_forbidden_targets(packet):
    with pytest.raises(egress.Denied):egress.parse_connect(packet,ALLOWED)

@pytest.mark.parametrize('ip',['127.0.0.1','10.0.0.1','100.64.0.10','169.254.169.254','192.168.1.1','172.16.0.1','0.0.0.0','224.0.0.1','::1','fe80::1','fc00::1','::ffff:8.8.8.8','2002:0808:0808::1'])
def test_private_and_transition_dns_rejected(monkeypatch,ip):
    monkeypatch.setattr(socket,'getaddrinfo',lambda *a,**k:[(socket.AF_INET6 if ':' in ip else socket.AF_INET,socket.SOCK_STREAM,6,'',(ip,443))])
    with pytest.raises(egress.Denied):egress.public_addresses('example.com')

def test_mixed_public_private_dns_denied(monkeypatch):
    monkeypatch.setattr(socket,'getaddrinfo',lambda *a,**k:[(socket.AF_INET,socket.SOCK_STREAM,6,'',(ip,443)) for ip in ['8.8.8.8','127.0.0.1']])
    with pytest.raises(egress.Denied):egress.public_addresses('example.com')

def test_connection_pins_resolved_ip(monkeypatch):
    connected=[]
    class Socket:
        def settimeout(self,value):pass
        def connect(self,value):connected.append(value)
    monkeypatch.setattr(socket,'getaddrinfo',lambda *a,**k:[(socket.AF_INET,socket.SOCK_STREAM,6,'',('8.8.8.8',443))])
    monkeypatch.setattr(socket,'socket',lambda *a,**k:Socket())
    egress.connect_public('example.com')
    assert connected==[('8.8.8.8',443)]

def test_real_listener_denies_private_without_outbound_connection(monkeypatch):
    def unexpected(*a,**k):raise AssertionError('must not connect')
    monkeypatch.setattr(egress,'connect_public',unexpected)
    with egress.Gateway(('127.0.0.1',0),ALLOWED) as server:
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        with socket.create_connection(server.server_address,timeout=2) as client:
            client.sendall(b'CONNECT 127.0.0.1:443 HTTP/1.1\r\n\r\n')
            assert client.recv(1024).startswith(b'HTTP/1.1 403')
        server.shutdown();thread.join(timeout=2)


def test_provider_response_after_31_seconds_is_not_cut_off(monkeypatch):
    from types import SimpleNamespace
    clock = [0.0]

    class Stream:
        def __init__(self, data):
            self.data = bytearray(data)
            self.sent = bytearray()
            self.closed = False
        def settimeout(self, value):
            pass
        def recv(self, count):
            chunk = bytes(self.data[:count])
            del self.data[:count]
            return chunk
        def sendall(self, data):
            self.sent.extend(data)
        def close(self):
            self.closed = True

    request = Stream(b'CONNECT api.anthropic.com:443 HTTP/1.1\r\nHost: api.anthropic.com:443\r\n\r\n')
    upstream = Stream(b'delayed-provider-response')
    monkeypatch.setattr(egress, 'connect_public', lambda *args: upstream)
    monkeypatch.setattr(egress.time, 'monotonic', lambda: clock[0])

    def select_ready(streams, write, errors, timeout):
        if clock[0] == 0:
            clock[0] += min(timeout, 31)
            return ([upstream] if timeout >= 31 else []), [], []
        return [request], [], []

    monkeypatch.setattr(egress.select, 'select', select_ready)
    egress.Handler(request, ('127.0.0.1', 12345), SimpleNamespace(allowed={'api.anthropic.com'}))
    assert request.sent.endswith(b'delayed-provider-response')
    assert upstream.closed
