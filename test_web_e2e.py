import asyncio
import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from discovery_state import DiscoveryStore
from form_assistant import prepare_form
from web_crawler import explore, fingerprint_distance


class FixtureHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        pages = {
            "/": """<html><title>Acme</title><body><h1>Acme builds developer tools</h1><script>setTimeout(()=>{let a=document.createElement('a');a.href='/careers';a.textContent='Careers';document.body.append(a)},30)</script></body></html>""",
            "/careers": """<html><title>Acme careers</title><body><h1>Work with us</h1><a href='/jobs/backend'>Backend Engineer opening</a><a href='/captcha'>Apply now</a></body></html>""",
            "/jobs/backend": """<html><title>Backend Engineer</title><body>Remote worldwide backend role. Go Python Kubernetes</body></html>""",
            "/captcha": """<html><title>Verify you are human</title><body>Please complete CAPTCHA</body></html>""",
            "/form": """<html><body><form><label>First name<input name='first' required></label><label>Email<input name='email' type='email' required></label><label>Resume<input name='resume' type='file' required></label><label>Visa sponsorship<input name='visa' required></label><button type='submit'>Apply</button></form></body></html>""",
            "/robots.txt": "User-agent: *\nAllow: /\n",
        }
        body = pages.get(self.path, "not found").encode()
        self.send_response(200 if self.path in pages else 404)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class BrowserFixtureTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), FixtureHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.origin = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.thread.join(timeout=3)

    async def test_dynamic_exploration_graph_and_human_gate(self):
        with tempfile.TemporaryDirectory() as directory:
            store = DiscoveryStore(Path(directory) / "crawl.sqlite")
            result = await explore([self.origin + "/"], store=store, max_pages=10, max_depth=4, delay=0, fixture=True)
            urls = {page["url"] for page in result["pages"]}
            self.assertIn(self.origin + "/careers", urls)
            self.assertIn(self.origin + "/jobs/backend", urls)
            self.assertTrue(any(cp["reason"] == "human_verification_or_access_gate" for cp in result["checkpoints"]))
            self.assertGreaterEqual(len(result["edges"]), 2)
            self.assertGreaterEqual(result["unique_states"], 2)
            self.assertGreaterEqual(store.recover_frontier(), 0)

    async def test_form_fill_upload_dry_run_and_unknown_checkpoint(self):
        with tempfile.TemporaryDirectory() as directory:
            resume = Path(directory) / "resume.pdf"
            resume.write_bytes(b"%PDF-1.4 fixture bytes")
            profile = {"facts": {"first_name": {"value": "Ada", "approved": True}, "email": {"value": "ada@example.com", "approved": True}, "visa": {"value": "no", "approved": True}}}
            dry = await prepare_form(self.origin + "/form", profile, resume, dry_run=True, fixture=True, authorize_resume_upload=True)
            self.assertFalse(dry["completed"])
            self.assertFalse(dry["submission_authorized"])
            self.assertTrue(dry["review"])
            result = await prepare_form(self.origin + "/form", profile, resume, dry_run=False, fixture=True, automation_permitted=True, authorize_resume_upload=True)
            self.assertEqual(len(result["completed"]), 3)
            self.assertTrue(any(row["reason"] == "unresolved field" for row in result["review"]))
            self.assertFalse(result["submission_authorized"])

    async def test_live_form_interaction_requires_explicit_permission(self):
        result = await prepare_form("https://example.com/apply", {}, dry_run=False)
        self.assertEqual(result["review"][0]["reason"], "automation_permission_not_confirmed")
        self.assertFalse(result["submission_authorized"])


if __name__ == "__main__":
    unittest.main()
