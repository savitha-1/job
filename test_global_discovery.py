import tempfile
import unittest
from pathlib import Path

from job_sources import normalize, freshness
from live_discovery import deduplicate
from opportunity_store import OpportunityStore
from relevance import score_job
from web_evidence import inspect_html


class GlobalDiscoveryTests(unittest.TestCase):
    def test_feed_listing_never_becomes_official_apply_link(self):
        payload = [{"id": "123", "position": "Backend Engineer", "company": "Example", "url": "https://remoteok.com/remote-jobs/123", "apply_url": "https://remoteok.com/remote-jobs/123", "description": "Go services"}]
        row = normalize("remoteok", payload)[0]
        self.assertIsNone(row["official_url"])
        self.assertFalse(row["vacancy_verified"])

    def test_jsonld_vacancy_page_is_separate_from_application(self):
        html = '''<script type="application/ld+json">{"@type":"JobPosting","title":"Backend Engineer","url":"/jobs/42"}</script>'''
        posting = inspect_html("https://acme.example/careers", html)["job_postings"][0]
        self.assertEqual(posting["vacancy_url"], "https://acme.example/jobs/42")
        self.assertIsNone(posting["application_url"])

    def test_alias_matching_and_role_gate(self):
        result = score_job({"title": "Backend Engineer", "description": "Services on Golang and PostgreSQL"}, ["Go", "Python"])
        self.assertEqual([x["skill"] for x in result["matches"]], ["Go"])
        self.assertTrue(result["relevant"])
        unrelated = score_job({"title": "Account Executive", "description": "Uses Python"}, ["Python"])
        self.assertFalse(unrelated["relevant"])

    def test_unknown_direct_apply_never_counts_as_eligible_or_official(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = OpportunityStore(Path(tmp) / "db.sqlite")
            row = {"source": "feed", "source_id": "1", "employer": "Acme", "title": "Backend Engineer", "location": "Remote", "description": "Python", "source_url": "https://board.example/jobs/1", "published_at": "2026-11-24T00:00:00+00:00", "vacancy_verified": True, "official_url": None, "official_url_verified": True}
            store.save_opportunity(row, ["Python"])
            stats = store.stats()
            self.assertEqual(stats["verified_vacancies"], 1)
            self.assertEqual(stats["official_apply_urls"], 0)
            self.assertEqual(stats["fresh_eligible_opportunities"], 0)

    def test_exact_cross_feed_vacancy_url_dedup_keeps_provenance(self):
        base = {"company": "Acme", "title": "Engineer", "source_listing_url": "https://jobs.example/1", "discovery_source": "Feed A", "india_eligibility": "unknown", "eligibility_evidence": []}
        duplicate = {**base, "discovery_source": "Feed B", "india_eligibility": "eligible_signal", "eligibility_evidence": [{"signal": "india_allowed"}]}
        rows = deduplicate([base, duplicate])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["also_seen_in"], ["Feed A", "Feed B"])
        self.assertEqual(rows[0]["india_eligibility"], "eligible_signal")

    def test_partial_source_run_is_not_persisted_as_total_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = OpportunityStore(Path(tmp) / "db.sqlite")
            run = store.begin_run()
            store.finish_run(run, {"feed": {"rows": 1}, "gharchive": {"error": "HTTPError"}}, "gharchive: HTTPError", state="partial")
            self.assertEqual(store.stats()["runs"][0]["state"], "partial")


if __name__ == "__main__":
    unittest.main()
