"""Run a bounded read-only pass across the configured international job feeds."""
import argparse
import csv
import datetime as dt
import json
import re
import time
from pathlib import Path
from urllib.parse import urlsplit

from engine import eligibility_evidence
from job_sources import SOURCES, fetch, freshness
from relevance import score_job


def discover(skills, sources=None, *, verification_state_path=None, max_verification_urls=60, max_verification_requests=240):
    started = time.monotonic()
    observed = dt.datetime.now(dt.timezone.utc).isoformat()
    results, errors, records, company_leads = {}, [], [], []
    for source in sources or list(SOURCES):
        before = time.monotonic()
        try:
            rows = fetch(source)
            relevant = []
            for row in rows:
                relevance = score_job(row, skills)
                if not relevance["relevant"] or freshness(row) == "stale":
                    continue
                eligibility, evidence = eligibility_evidence(row.get("location"), row.get("description"), title=row.get("title"))
                keyword_score = sum(bool(re.search(r"(?<!\w)" + re.escape(str(skill)) + r"(?!\w)", (row.get("title", "") + " " + row.get("description", "")), re.I)) for skill in skills)
                result = {"company": row["employer"], "official_website": None,
                          "title": row["title"], "source_listing_url": row["source_url"],
                          "direct_application_url": row.get("official_url"),
                          "work_location": row.get("location") or "Unknown",
                          "india_eligibility": eligibility, "eligibility_evidence": evidence,
                          "technical_matches": relevance["matches"], "relevance_score": relevance["score"],
                          "keyword_baseline_score": keyword_score,
                          "compensation": "Unknown", "published_at": row.get("published_at"),
                          "observed_at": row.get("observed_at") or observed,
                          "discovery_source": row["source_name"], "source_credit": row["source_credit"],
                          "source_status": "present in current public provider feed; not independently verified with employer",
                          "official_employer_application_verified": False}
                relevant.append(result)
            records.extend(relevant)
            results[source] = {"rows_received": len(rows), "relevant_current_roles": len(relevant),
                               "distinct_employers": len({x["company"].casefold() for x in relevant}),
                               "india_eligibility_signals": sum(x["india_eligibility"] == "eligible_signal" for x in relevant),
                               "direct_application_urls": sum(bool(x["direct_application_url"]) for x in relevant),
                               "elapsed_seconds": round(time.monotonic() - before, 3)}
        except Exception as exc:
            errors.append({"source": source, "error": type(exc).__name__})
            results[source] = {"error": type(exc).__name__}
    # A monthly employer-authored hiring thread provides a different source
    # class from aggregators. Parse it as leads only: no claim is promoted to
    # verified without corroboration on an employer-controlled vacancy page.
    try:
        from hn_hiring import fetch_latest
        hn = fetch_latest(max_comments=1200)
        hn_rows = hn["opportunities"]
        hn_relevant = []
        for row in hn_rows:
            relevance = score_job(row, skills)
            if not relevance["relevant"]:
                continue
            eligibility, evidence = eligibility_evidence(row.get("location"), row.get("description"), title=row.get("title"))
            hn_relevant.append({"company": row["employer"], "official_website": None, "title": row["title"],
                "source": row["source"], "source_id": row["source_id"], "employer": row["employer"],
                "source_listing_url": row["source_url"], "direct_application_url": row.get("official_url"),
                "candidate_application_urls": row.get("candidate_urls", []), "work_location": row.get("location") or "Unknown",
                "india_eligibility": eligibility, "eligibility_evidence": evidence, "technical_matches": relevance["matches"],
                "relevance_score": relevance["score"], "keyword_baseline_score": sum(bool(re.search(r"(?<!\w)" + re.escape(str(skill)) + r"(?!\w)", row.get("title", "") + " " + row.get("description", ""), re.I)) for skill in skills),
                "compensation": "Unknown", "published_at": row.get("published_at"), "observed_at": observed,
                "discovery_source": row["source_name"], "source_credit": row["source_credit"],
                "source_status": "public employer-submitted hiring-thread lead; not independently verified against employer",
                "official_employer_application_verified": False, "thread_url": row.get("thread_url"), "source_evidence": row.get("evidence")})
        if verification_state_path is None:
            verification_state_path = Path.home() / ".cache" / "job-intelligence" / "verification.sqlite3"
        from verification_pipeline import run_verification_batch
        verified_batch = run_verification_batch(hn_relevant, verification_state_path, skills,
            max_urls=max_verification_urls, max_requests=max_verification_requests)
        hn_verified = verified_batch["verified_opportunities"]
        records.extend(hn_relevant)
        records.extend(hn_verified)
        results["hn_whoishiring"] = {"thread": hn.get("thread"), "comments_read": hn["comments_read"],
            "top_level_comments": hn.get("top_level_comments", 0), "engineering_role_leads": len(hn_relevant),
            "candidate_external_links": sum(bool(x["candidate_application_urls"]) for x in hn_relevant),
            "verification_urls_processed_this_run": verified_batch["processed_urls"],
            "network_requests": verified_batch["requests_used"], "bytes_read": verified_batch["bytes_read"],
            "elapsed_seconds": verified_batch["elapsed_seconds"],
            "verification_failure_reasons": verified_batch["failure_reasons"],
            "verification_frontier": verified_batch["frontier"],
            "verified_employer_vacancies": len(hn_verified), "verified_official_application_urls": len(hn_verified)}
    except Exception as exc:
        errors.append({"source": "hn_whoishiring", "error": type(exc).__name__})
        results["hn_whoishiring"] = {"error": type(exc).__name__}
    # Company discovery is independent of the job boards. GH Archive events are
    # only leads; GitHub's public organization endpoint does not establish that
    # the account is an employer or that a vacancy exists.
    try:
        from gharchive_live import fetch_hour, resolve_public_organization
        activity = fetch_hour(compressed_limit=8 * 1024 * 1024, expanded_limit=48 * 1024 * 1024, max_events=60_000)
        ranked = sorted(activity["leads"], key=lambda x: (x["hiring_signals"], sum(x["technologies"].values()), x["event_count"]), reverse=True)
        lookup_errors = 0
        non_organizations = 0
        organization_lookups_attempted = 0
        rate_limit_reset = None
        for lead in ranked[:12]:
            organization_lookups_attempted += 1
            try:
                org = resolve_public_organization(lead["organization"])
            except Exception as exc:
                lookup_errors += 1
                from gharchive_live import GitHubRateLimited
                if isinstance(exc, GitHubRateLimited):
                    rate_limit_reset = exc.reset_at
                    break
                continue
            if org:
                company_leads.append({"name": org["name"], "github_organization": org["login"],
                                      "homepage_claim": org["homepage_claim"], "location_claim": org["location"],
                                      "public_repositories": org["public_repos"], "activity_events_in_hour": lead["event_count"],
                                      "employer_verified": False, "vacancies_verified": 0,
                                      "source": org["evidence_url"]})
            else:
                non_organizations += 1
        results["gharchive_live"] = {"hour_utc": activity["hour_utc"], "events_read": activity["events_read"],
                                      "activity_company_leads": len(activity["leads"]),
                                      "organization_lookups_attempted": organization_lookups_attempted,
                                      "organization_profiles_resolved": len(company_leads),
                                      "not_organization_or_not_found": non_organizations,
                                      "lookup_errors": lookup_errors,
                                      "rate_limited": rate_limit_reset is not None,
                                      "retry_at": rate_limit_reset,
                                      "employer_verified": 0, "vacancies_verified": 0}
    except Exception as exc:
        errors.append({"source": "gharchive_live", "error": type(exc).__name__})
        results["gharchive_live"] = {"error": type(exc).__name__}
    source_observations = len(records)
    unique = deduplicate(records)
    unique.sort(key=lambda x: (x["india_eligibility"] != "eligible_signal", x["relevance_score"] * -1, x["company"].casefold(), x["title"].casefold()))
    rejected = [row for row in unique if row["india_eligibility"] == "excluded"]
    opportunities = [row for row in unique if row["india_eligibility"] not in ("excluded", "conflicted")]
    def top20(rows, key):
        chosen = sorted(rows, key=key, reverse=True)[:20]
        return {"rows": len(chosen), "distinct_employer_name_strings": len({x["company"].casefold() for x in chosen}),
                "india_eligibility_text_signals": sum(x["india_eligibility"] == "eligible_signal" for x in chosen),
                "official_apply_urls": sum(bool(x["direct_application_url"]) and x["official_employer_application_verified"] for x in chosen)}
    comparison = {"sample_size": len(unique), "top_20_field_weighted": top20(unique, lambda x: (x["india_eligibility"] == "eligible_signal", x["relevance_score"])),
                  "top_20_ordinary_keyword": top20(unique, lambda x: (x["india_eligibility"] == "eligible_signal", x["keyword_baseline_score"])),
                  "human_labeled_ground_truth": 0,
                  "interpretation": "Descriptive reranking on the same live feed sample; eligibility labels are heuristic and this is not a precision or accuracy estimate."}
    return {"observed_at": observed, "elapsed_seconds": round(time.monotonic() - started, 3),
            "source_results": results, "errors": errors,
            "descriptive_comparison": comparison,
            "totals": {"source_observations": source_observations,
                       "unique_provider_listings": len(unique),
                       "distinct_employer_name_strings": len({x["company"].casefold() for x in unique}),
                       "verified_employers": len({x["company"].casefold() for x in unique if x["official_employer_application_verified"]}),
                       "unverified_employer_name_leads": len({x["company"].casefold() for x in unique}),
                       "fully_verified_india_eligible_opportunities": sum(x["india_eligibility"] == "eligible_signal" and x["official_employer_application_verified"] for x in unique),
                       "provider_listings_observed": len(unique),
                       "official_employer_application_urls": sum(bool(x["direct_application_url"]) and x["official_employer_application_verified"] for x in unique),
                       "verified_active_vacancies": sum(bool(x.get("vacancy_verified")) for x in unique),
                       "india_eligibility_signals": sum(x["india_eligibility"] == "eligible_signal" for x in unique),
                       "unknown_eligibility": sum(x["india_eligibility"] == "unknown" for x in unique),
                       "excluded": len(rejected),
                       "ranked_investigation_listings": len(opportunities),
                       "prepared_applications": 0,
                       "confirmed_submissions": 0,
                       "resolved_unverified_github_organization_leads": len(company_leads)}, "opportunities": opportunities,
            "company_leads": company_leads,
            "rejected_opportunities": rejected}


def deduplicate(records):
    """Merge exact posting URL sightings across providers, retaining evidence."""
    result, by_key = [], {}
    for original in records:
        row = dict(original)
        key = (row["company"].casefold(), row["source_listing_url"].rstrip("/").casefold())
        if key in by_key:
            prior = by_key[key]
            prior["also_seen_in"] = sorted(set(prior.get("also_seen_in", [prior["discovery_source"]])) | {row["discovery_source"]})
            observations = prior.setdefault("eligibility_observations", [{"source": prior["discovery_source"], "status": prior["india_eligibility"], "evidence": prior.get("eligibility_evidence", [])}])
            observations.append({"source": row["discovery_source"], "status": row["india_eligibility"], "evidence": row.get("eligibility_evidence", [])})
            states = {item["status"] for item in observations}
            if "excluded" in states and "eligible_signal" in states:
                prior["india_eligibility"] = "conflicted"
            elif states == {"eligible_signal"}:
                prior["india_eligibility"] = "eligible_signal"
            elif "excluded" in states:
                prior["india_eligibility"] = "excluded"
            else:
                prior["india_eligibility"] = "unknown"
            prior["eligibility_evidence"] = [ev for item in observations for ev in item["evidence"]]
            continue
        row["also_seen_in"] = [row["discovery_source"]]
        by_key[key] = row
        result.append(row)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default="reports/latest-discovery.json")
    parser.add_argument("--csv-output", default="reports/latest-discovery.csv")
    parser.add_argument("--verification-db", default=str(Path.home() / ".cache" / "job-intelligence" / "verification.sqlite3"))
    parser.add_argument("--max-verification-urls", type=int, default=60)
    parser.add_argument("--max-verification-requests", type=int, default=240)
    parser.add_argument("--source", action="append", choices=list(SOURCES))
    parser.add_argument("--skill", action="append", default=[])
    args = parser.parse_args()
    skills = args.skill or ["Go", "Python", "TypeScript", "Node.js", "React", "Kubernetes", "PostgreSQL", "AWS"]
    report = discover(skills, args.source, verification_state_path=args.verification_db,
                      max_verification_urls=args.max_verification_urls,
                      max_verification_requests=args.max_verification_requests)
    target = Path(args.output)
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    csv_target = Path(args.csv_output)
    csv_target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    columns = ["company", "official_website", "title", "direct_application_url", "source_listing_url", "published_at", "observed_at", "work_location", "india_eligibility", "eligibility_evidence", "technical_matches", "relevance_score", "compensation", "discovery_source", "source_status", "official_employer_application_verified"]
    with csv_target.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in report["opportunities"]:
            writer.writerow({**row, "eligibility_evidence": json.dumps(row.get("eligibility_evidence", []), ensure_ascii=False), "technical_matches": json.dumps(row.get("technical_matches", []), ensure_ascii=False)})
    print(json.dumps({"output": str(target), "csv_output": str(csv_target), "totals": report["totals"], "source_results": report["source_results"], "errors": report["errors"]}, indent=2))
    return int(bool(report["errors"]))


if __name__ == "__main__":
    raise SystemExit(main())
