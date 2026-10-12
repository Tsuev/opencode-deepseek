"""Optional CLI-only IPv4 transport for a broken IPv6 socket on the same network.

CONNECT tunnels preserve Google's TLS. No OAuth payload is read or logged, and
no DNS, system proxy, VPN, location, or browser setting is changed.
"""

from __future__ import annotations

import os
import select
import shutil
import socket
import subprocess
import sys
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

def allowed_host(host):
    return (host == "googleapis.com" or host.endswith(".googleapis.com")
            or host in ("antigravity.google", "antigravity.google.com")
            or host.endswith((".antigravity.google", ".antigravity.google.com")))


def connect_ipv4(host):
    for address in socket.getaddrinfo(host, 443, socket.AF_INET, socket.SOCK_STREAM):
        connection = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        connection.settimeout(8)
        try:
            connection.connect(address[4])
            return connection
        except OSError:
            connection.close()
    raise OSError("Google IPv4 connection failed")


class Tunnel(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_):
        pass

    def do_CONNECT(self):
        host, separator, port = self.path.rpartition(":")
        if (not separator or port != "443" or not host or host != host.lower()
                or any(char not in "abcdefghijklmnopqrstuvwxyz0123456789.-" for char in host)
                or not allowed_host(host)):
            self.send_error(403, "Only official Google TLS destinations are allowed")
            return
        try:
            upstream = connect_ipv4(host)
        except OSError:
            self.send_error(502, "Google IPv4 connection failed")
            return
        with upstream:
            self.send_response(200, "Connection Established")
            self.end_headers()
            self.connection.settimeout(8)
            sockets = (self.connection, upstream)
            while not self.server.stopping.is_set():
                readable, _, exceptional = select.select(sockets, [], sockets, .5)
                if exceptional:
                    break
                try:
                    for source in readable:
                        data = source.recv(65536)
                        if not data:
                            return
                        destination = upstream if source is self.connection else self.connection
                        destination.sendall(data)
                except OSError:
                    return
        self.close_connection = True


@contextmanager
def transport():
    # An existing configured proxy may determine the user's region. Never
    # override it with a direct connection as part of this local workaround.
    if any(os.getenv(name) for name in ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "https_proxy", "http_proxy", "all_proxy")):
        raise RuntimeError("Keep your configured proxy; the IPv4 helper will not override it")
    server = ThreadingHTTPServer(("127.0.0.1", 0), Tunnel)
    server.daemon_threads = True
    server.stopping = threading.Event()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    proxy = "http://127.0.0.1:" + str(server.server_port)
    try:
        yield {**os.environ, "HTTPS_PROXY":proxy, "HTTP_PROXY":proxy,
               "NO_PROXY":"localhost,127.0.0.1", "no_proxy":"localhost,127.0.0.1"}
    finally:
        server.stopping.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def main():
    binary = os.getenv("ANTIGRAVITY_OFFICIAL_BIN") or shutil.which("agy")
    if not binary:
        candidate = Path.home() / ".local" / "bin" / "agy"
        if candidate.is_file():
            binary = str(candidate)
    if not binary:
        raise RuntimeError("Install the official Antigravity CLI first")
    with transport() as environment:
        process = subprocess.Popen([binary, *sys.argv[1:]], env=environment)
        try:
            return process.wait()
        except KeyboardInterrupt:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            return 130


if __name__ == "__main__":
    raise SystemExit(main())
