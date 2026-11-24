import gzip
import io
import json
import tempfile
import unittest
from pathlib import Path

from activity_discovery import ingest_events
from discovery_state import DiscoveryStore
from gmail_tracking import correlate_confirmations, sync_confirmations
from ledger import Ledger
from network_guard import origin, resolve_public
from web_evidence import inspect_html


class _FakeHTTPResponse(io.BytesIO):
    status = 200
    headers = {"Content-Length": "2"}


class FakeMailbox:
    def __init__(self, messages):
        self.messages = messages
        self.queries = []

    def search(self, query, limit=100):
        self.queries.append((query, limit))
        return self.messages[:limit]


class IntegrationTests(unittest.TestCase):
    def test_common_crawl_success_cache_prevents_repeat_fetch(self):
        from unittest.mock import patch
        import archive_discovery
        response = _FakeHTTPResponse(b"{}")
        opener = type("Opener", (), {"open": lambda self, *a, **k: response})()
        with tempfile.TemporaryDirectory() as d, patch.object(archive_discovery, "build_public_opener", return_value=opener) as make_opener, patch.object(archive_discovery, "PublicHTTPSProxy") as proxy:
            proxy.return_value.__enter__.return_value.url = "http://127.0.0.1:1234"
            self.assertEqual(archive_discovery.get("https://index.commoncrawl.org/test", cache_dir=d), "{}")
            self.assertEqual(archive_discovery.get("https://index.commoncrawl.org/test", cache_dir=d), "{}")
            self.assertEqual(make_opener.call_count, 1)

    def test_public_activity_lead_is_provenanced_and_unverified(self):
        row = {"type": "PushEvent", "created_at": "2026-11-24T00:00:00Z", "org": {"login": "acme"}, "repo": {"name": "acme/platform"}, "payload": {"commits": [{"message": "Python and Kubernetes backend"}]}}
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "events.json.gz"
            with gzip.open(path, "wt", encoding="utf-8") as out:
                out.write(json.dumps(row) + "\n")
            result = ingest_events(path)
        lead = result["leads"][0]
        self.assertEqual(lead["organization"], "acme")
        self.assertFalse(lead["verified_employer"])
        self.assertIn("Python", lead["technologies"])
        self.assertIn("Kubernetes", lead["technologies"])

    def test_repository_user_is_not_promoted_to_employer_lead(self):
        from io import BytesIO
        from activity_discovery import ingest_stream
        row = {"type": "PushEvent", "repo": {"name": "alice/project"}, "payload": {"commits": [{"message": "Hiring Python engineer"}]}}
        self.assertEqual(ingest_stream(BytesIO((json.dumps(row) + "\n").encode()))["leads"], [])

    def test_commit_message_does_not_count_as_hiring_signal(self):
        from io import BytesIO
        from activity_discovery import ingest_stream
        row = {"type": "PushEvent", "org": {"login": "acme"}, "repo": {"name": "acme/project"}, "payload": {"commits": [{"message": "We are hiring Python engineers"}]}}
        result = ingest_stream(BytesIO((json.dumps(row) + "\n").encode()))
        self.assertEqual(result["leads"][0]["hiring_signals"], 0)

    def test_jsonld_job_posting_and_career_links(self):
        html = '<a href="/careers">Jobs</a><script type="application/ld+json">{"@type":"JobPosting","title":"Backend Engineer","applicantLocationRequirements":{"@type":"Country","name":"India"}}</script>'
        result = inspect_html("https://example.com/", html)
        self.assertEqual(len(result["job_postings"]), 1)
        self.assertEqual(result["career_links"], ["https://example.com/careers"])

    def test_gmail_correlation_updates_only_unambiguous_confirmation(self):
        with tempfile.TemporaryDirectory() as d:
            ledger = Ledger(Path(d) / "ledger.sqlite")
            ledger.enqueue("https://careers.acme.example/jobs/1", "Acme")
            mailbox = FakeMailbox([{"id": "m1", "subject": "Application received at Acme", "snippet": "Thanks", "date": "today"}])
            result = sync_confirmations(mailbox, ledger)
            self.assertEqual(result["updated"], ["https://careers.acme.example/jobs/1"])
            self.assertEqual(ledger.rows()[0]["state"], "confirmed")
            self.assertFalse(result["daily_summary"]["external_message_sent"])

    def test_frontier_recovery_and_persistent_host_budget(self):
        with tempfile.TemporaryDirectory() as d:
            store = DiscoveryStore(Path(d) / "state.sqlite")
            store.enqueue("https://example.com/one")
            store.take_records(limit=1)
            self.assertEqual(store.recover_frontier(), 1)
            self.assertEqual(store.take(limit=1), ["https://example.com/one"])
            self.assertTrue(store.reserve_host_request("example.com", max_requests=2))
            self.assertTrue(store.reserve_host_request("example.com", max_requests=2))
            self.assertFalse(store.reserve_host_request("example.com", max_requests=2))

    def test_network_guard_rejects_private_dns_and_normalizes_origins(self):
        private_resolver = lambda *args, **kwargs: [(2, 1, 6, "", ("127.0.0.1", 443))]
        with self.assertRaises(PermissionError):
            resolve_public("internal.example", resolver=private_resolver)
        public_resolver = lambda *args, **kwargs: [(2, 1, 6, "", ("8.8.8.8", 443))]
        self.assertEqual(resolve_public("public.example", resolver=public_resolver)[0][4][0], "8.8.8.8")
        self.assertEqual(origin("https://Example.com/path"), "https://example.com:443")


if __name__ == "__main__":
    unittest.main()
