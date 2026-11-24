import http.client
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer

from dashboard import ProductService, make_handler


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = ProductService(self.temp.name)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.service))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temp.cleanup()

    def request(self, path, *, method="GET", body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=3)
        conn.request(method, path, body=body, headers=headers or {})
        response = conn.getresponse()
        payload = response.read()
        conn.close()
        return response.status, payload

    def test_loopback_dashboard_and_csrf_protected_profile(self):
        status, html = self.request("/")
        self.assertEqual(status, 200)
        self.assertIn(b"Start discovery", html)
        self.assertNotIn(b"Connect Gmail", html)
        csrf = self.service.csrf_token
        status, _ = self.request("/api/profile", method="POST", body=b'{"facts":{}}', headers={"Content-Type": "application/json"})
        self.assertEqual(status, 403)
        status, _ = self.request("/api/profile", method="POST", body=b'{"facts":{}}', headers={"Content-Type": "application/json", "X-CSRF-Token": csrf})
        self.assertEqual(status, 200)
        status, state = self.request("/api/state")
        self.assertEqual(status, 200)
        self.assertIn(b'"source_active_vacancies": 0', state)


if __name__ == "__main__":
    unittest.main()
