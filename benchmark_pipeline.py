"""Synthetic full-path throughput: HTML evidence -> eligibility -> form plan -> store.

This benchmarks application logic on deterministic fixtures. It is not a live
internet discovery rate or a claim about daily vacancy supply.
"""
import argparse
import json
import tempfile
import time
from pathlib import Path

from application_planner import plan
from discovery_state import DiscoveryStore
from engine import eligibility, rank
from web_evidence import inspect_html


def run(count=3000):
    if count < 1:
        raise ValueError("count must be positive")
    profile = {"first_name": "Candidate", "email": "candidate@example.invalid"}
    fields = [{"id": "first", "label": "First name", "type": "text", "required": True}]
    start = time.perf_counter()
    with tempfile.TemporaryDirectory() as d:
        store = DiscoveryStore(Path(d) / "pipeline.sqlite")
        jobs = []
        discovered = prepared = 0
        for i in range(count):
            domain = f"employer-{i}.example"
            url = f"https://{domain}/careers/backend"
            title = "Backend Engineer"
            body = f"Remote worldwide. Go Python Kubernetes requisition {i}."
            html = f'<a href="/careers">Careers</a><script type="application/ld+json">{{"@type":"JobPosting","title":"{title}","url":"{url}"}}</script>'
            evidence = inspect_html(url, html)
            employer = store.add_employer(domain, f"Employer {i}")
            lead = {"organization": f"Employer {i}", "domain": domain, "verified_employer": False, "evidence": [{"url": url, "kind": "fixture_career_page"}]}
            store.save_company_lead(lead, "fixture_crawl")
            job = {"source": "fixture", "board": domain, "id": str(i), "title": title, "location": "Remote", "description": body, "application_url": url}
            jobs.append(job)
            store.save_job(employer, job)
            prepared += int(plan(fields, profile)["ready"])
            discovered += len(evidence["job_postings"])
        ranked = rank(jobs, ["Go", "Python", "Kubernetes"])
        eligible = sum(eligibility(j["location"], j["description"]) == "eligible_signal" for j in ranked)
        elapsed = time.perf_counter() - start
    return {"records": count, "unique_employers": count, "structured_job_postings": discovered, "form_review_packets": prepared, "eligible_signals": eligible, "seconds": round(elapsed, 4), "records_per_second": round(count / elapsed, 2), "linear_records_per_day": round(count / elapsed * 86400), "input": "synthetic deterministic HTML fixtures", "network_requests": 0, "submissions": 0}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(); parser.add_argument("--count", type=int, default=3000)
    print(json.dumps(run(parser.parse_args().count), indent=2))
