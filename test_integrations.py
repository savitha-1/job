import gzip
import json
import tempfile
import unittest
from pathlib import Path

from activity_discovery import ingest_events
from discovery_state import DiscoveryStore
from gmail_tracking import correlate_confirmations, sync_confirmations
from ledger import Ledger
from web_evidence import inspect_html


class FakeMailbox:
    def __init__(self, messages):
        self.messages = messages
        self.queries = []

    def search(self, query, limit=100):
        self.queries.append((query, limit))
        return self.messages[:limit]


class IntegrationTests(unittest.TestCase):
    def test_public_activity_lead_is_provenanced_and_unverified(self):
        row = {"type": "PushEvent", "created_at": "2026-11-24T00:00:00Z", "repo": {"name": "acme/platform"}, "payload": {"commits": [{"message": "Python and Kubernetes backend"}]}}
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


if __name__ == "__main__":
    unittest.main()
