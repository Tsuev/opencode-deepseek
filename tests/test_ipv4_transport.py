import os
import socket
import unittest
from unittest.mock import patch

from providers.antigravity_ipv4 import allowed_host, transport


class IPv4TransportTests(unittest.TestCase):
    def test_existing_proxy_is_preserved(self):
        with patch.dict(os.environ,{"HTTPS_PROXY":"http://existing.example:8080"}):
            with self.assertRaises(RuntimeError), transport():
                pass

    def test_tunnel_never_connects_to_an_arbitrary_destination(self):
        with patch.dict(os.environ,{},clear=True), patch("providers.antigravity_ipv4.connect_ipv4") as connect:
            with transport() as environment:
                from urllib.parse import urlsplit
                endpoint = urlsplit(environment["HTTPS_PROXY"])
                with socket.create_connection((endpoint.hostname,endpoint.port),timeout=2) as client:
                    client.sendall(b"CONNECT example.org:443 HTTP/1.1\r\nHost: example.org:443\r\n\r\n")
                    self.assertIn(b"403", client.recv(512).split(b"\r\n")[0])
            connect.assert_not_called()

    def test_google_host_allowlist_cannot_be_suffix_spoofed(self):
        self.assertTrue(allowed_host("oauth2.googleapis.com"))
        self.assertTrue(allowed_host("antigravity.google"))
        for host in ("evilgoogleapis.com","googleapis.com.evil.test","evil.test","localhost"):
            self.assertFalse(allowed_host(host))
