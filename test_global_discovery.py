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

    def test_ordinary_go_verb_is_not_golang_evidence(self):
        row = {"title": "Backend Engineer", "description": "We go above and beyond building software"}
        self.assertEqual(score_job(row, ["Go"])["matches"], [])
        self.assertEqual(score_job({"title": "Go Engineer"}, ["Go"])["matches"][0]["matched_as"], "go")

    def test_hn_comment_is_unverified_lead_with_candidate_link_and_redacted_contact(self):
        from hn_hiring import parse_comment
        hit = {"objectID": "991", "created_at": "2026-11-01T00:00:00Z", "comment_text": "Acme | Backend Engineer | Remote | India<p>Apply at https://jobs.acme.example/42 or email hiring@acme.example"}
        row = parse_comment(hit, "900")
        self.assertEqual(row["employer"], "Acme")
        self.assertEqual(row["title"], "Backend Engineer")
        self.assertIn("India", row["location"])
        self.assertIsNone(row["official_url"])
        self.assertEqual(row["candidate_urls"], ["https://jobs.acme.example/42"])
        self.assertFalse(row["official_url_verified"])
        self.assertIn("[email removed]", row["description"])

    def test_hn_feed_drops_nested_replies(self):
        from unittest.mock import patch
        from hn_hiring import fetch_latest
        story = {"hits": [{"objectID": "900", "title": "Ask HN: Who is hiring? (November 2026)", "created_at": "2026-11-01T00:00:00Z"}]}
        comments = {"hits": [
            {"objectID": "901", "parent_id": 900, "created_at": "2026-11-01T00:01:00Z", "comment_text": "Acme | Backend Engineer | India<p>Go services"},
            {"objectID": "902", "parent_id": 901, "created_at": "2026-11-01T00:02:00Z", "comment_text": "I am a Backend Engineer looking for work"},
        ], "nbPages": 1}
        with patch("hn_hiring._request_json", side_effect=[story, comments]):
            result = fetch_latest(max_comments=20)
        self.assertEqual(result["comments_read"], 2)
        self.assertEqual(result["top_level_comments"], 1)
        self.assertEqual([x["employer"] for x in result["opportunities"]], ["Acme"])

    def test_hn_verifier_blocks_cross_origin_redirects(self):
        from hn_hiring import _SameOriginRedirects
        from urllib.error import HTTPError
        from types import SimpleNamespace
        handler = _SameOriginRedirects()
        with self.assertRaises(HTTPError) as caught:
            handler.redirect_request(SimpleNamespace(full_url="https://company.example/jobs/1"), None, 302, "Found", {}, "https://external.example/apply/1")
        caught.exception.close()

    def test_unknown_direct_apply_never_counts_as_eligible_or_official(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = OpportunityStore(Path(tmp) / "db.sqlite")
            row = {"source": "feed", "source_id": "1", "employer": "Acme", "title": "Backend Engineer", "location": "Remote", "description": "Python", "source_url": "https://board.example/jobs/1", "published_at": "2026-11-24T00:00:00+00:00", "vacancy_verified": True, "official_url": None, "official_url_verified": True}
            store.save_opportunity(row, ["Python"])
            stats = store.stats()
            self.assertEqual(stats["verified_vacancies"], 1)
            self.assertEqual(stats["official_apply_urls"], 0)
            self.assertEqual(stats["fresh_eligible_opportunities"], 0)

    def test_unverified_same_name_employers_are_not_merged(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = OpportunityStore(Path(tmp) / "db.sqlite")
            common = {"employer": "Acme", "title": "Backend Engineer", "location": "India", "description": "Python", "published_at": "2026-11-24T00:00:00+00:00"}
            for source, source_id, url in (("feed_a", "1", "https://board-a.example/jobs/1"), ("feed_b", "2", "https://board-b.example/jobs/2")):
                store.save_opportunity({**common, "source": source, "source_id": source_id, "source_url": url}, ["Python"])
            stats = store.stats()
            self.assertEqual(stats["employers_discovered"], 0)
            self.assertEqual(stats["unverified_company_leads"], 2)

    def test_exact_cross_feed_vacancy_url_dedup_keeps_provenance(self):
        base = {"company": "Acme", "title": "Engineer", "source_listing_url": "https://jobs.example/1", "discovery_source": "Feed A", "india_eligibility": "excluded", "eligibility_evidence": [{"signal": "us_only"}]}
        duplicate = {**base, "discovery_source": "Feed B", "india_eligibility": "eligible_signal", "eligibility_evidence": [{"signal": "india_allowed"}]}
        rows = deduplicate([base, duplicate])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["also_seen_in"], ["Feed A", "Feed B"])
        self.assertEqual(rows[0]["india_eligibility"], "conflicted")
        self.assertEqual(len(rows[0]["eligibility_observations"]), 2)

    def test_partial_source_run_is_not_persisted_as_total_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = OpportunityStore(Path(tmp) / "db.sqlite")
            run = store.begin_run()
            store.finish_run(run, {"feed": {"rows": 1}, "gharchive": {"error": "HTTPError"}}, "gharchive: HTTPError", state="partial")
            self.assertEqual(store.stats()["runs"][0]["state"], "partial")


if __name__ == "__main__":
    unittest.main()
