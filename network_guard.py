"""Loopback HTTPS CONNECT proxy that resolves and connects to public IPs only.

The proxy pins each connection to the exact address returned by its DNS lookup,
rejects mixed/private DNS answers, permits only port 443, and limits idle time,
wall time and tunnel bytes. TLS remains end-to-end between Chromium and origin.
"""
import ipaddress
import select
import socket
import socketserver
import threading
import time
from urllib.request import build_opener, ProxyHandler
from urllib.parse import urlsplit


def resolve_public(host, port=443, resolver=socket.getaddrinfo):
    try:
        literal = ipaddress.ip_address(host)
        addresses = [(socket.AF_INET6 if literal.version == 6 else socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (str(literal), port))]
    except ValueError:
        addresses = resolver(host, port, type=socket.SOCK_STREAM)
    parsed = []
    for family, socktype, proto, _, sockaddr in addresses:
        ip = ipaddress.ip_address(sockaddr[0])
        if not ip.is_global:
            raise PermissionError("non-public DNS address rejected")
        if (family, sockaddr) not in [(entry[0], entry[4]) for entry in parsed]:
            parsed.append((family, socktype, proto, "", sockaddr))
    if not parsed:
        raise OSError("DNS returned no addresses")
    return parsed


class _Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class _Handler(socketserver.StreamRequestHandler):
    timeout = 10

    def handle(self):
        upstream = None
        try:
            first = self.rfile.readline(8193).decode("ascii", "strict").strip()
            parts = first.split()
            if len(parts) != 3 or parts[0].upper() != "CONNECT" or parts[2] not in ("HTTP/1.0", "HTTP/1.1"):
                self.wfile.write(b"HTTP/1.1 403 Forbidden\r\nConnection: close\r\n\r\n")
                return
            authority = parts[1]
            if authority.startswith("["):
                host, _, raw_port = authority[1:].partition("]:")
            else:
                host, _, raw_port = authority.rpartition(":")
            if not host or len(host) > 253 or raw_port != "443" or any(c in host for c in "@/\\\x00\r\n"):
                self.wfile.write(b"HTTP/1.1 403 Forbidden\r\nConnection: close\r\n\r\n")
                return
            while True:
                header = self.rfile.readline(8193)
                if not header or header in (b"\r\n", b"\n"):
                    break
                if len(header) > 8192:
                    raise ValueError("proxy header too large")
            targets = resolve_public(host, 443)
            family, socktype, proto, _, sockaddr = targets[0]
            upstream = socket.socket(family, socktype, proto)
            upstream.settimeout(10)
            upstream.connect(sockaddr)  # connect to the validated IP, never resolve again
            self.wfile.write(b"HTTP/1.1 200 Connection Established\r\nProxy-Agent: LocalOpportunityGuard\r\n\r\n")
            self.connection.setblocking(False)
            upstream.setblocking(False)
            started = time.monotonic()
            transferred = 0
            while time.monotonic() - started < 90:
                readable, _, _ = select.select([self.connection, upstream], [], [], 1)
                if not readable:
                    continue
                for source in readable:
                    data = source.recv(65536)
                    if not data:
                        return
                    transferred += len(data)
                    if transferred > 32 * 1024 * 1024:
                        return
                    (upstream if source is self.connection else self.connection).sendall(data)
        except (OSError, ValueError, UnicodeError, PermissionError):
            try:
                self.wfile.write(b"HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\n\r\n")
            except OSError:
                pass
        finally:
            if upstream:
                upstream.close()


class PublicHTTPSProxy:
    def __init__(self):
        self.server = _Server(("127.0.0.1", 0), _Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self):
        host, port = self.server.server_address
        return f"http://{host}:{port}"

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


def public_url(url):
    p = urlsplit(url)
    if p.scheme != "https" or not p.hostname or p.username or p.password or p.port not in (None, 443):
        return False
    try:
        ip = ipaddress.ip_address(p.hostname)
    except ValueError:
        return True  # DNS is checked and pinned by PublicHTTPSProxy on connect.
    return ip.is_global


def origin(url):
    p = urlsplit(url)
    if not p.scheme or not p.hostname:
        return None
    port = p.port or (443 if p.scheme == "https" else 80)
    return f"{p.scheme.lower()}://{p.hostname.lower()}:{port}"


def build_public_opener(proxy_url=None):
    """Return an urllib opener that uses the public-IP-pinning proxy."""
    return build_opener(ProxyHandler({"https": proxy_url, "http": proxy_url}) if proxy_url else ProxyHandler({}))
