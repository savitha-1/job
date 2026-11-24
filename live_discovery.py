"""Run a bounded read-only pass across the configured international job feeds."""
import argparse
import datetime as dt
import json
import re
import time
from pathlib import Path

from engine import eligibility_evidence
from job_sources import SOURCES, fetch, freshness
from relevance import score_job


def discover(skills, sources=None):
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
    # Company discovery is independent of the job boards. GH Archive events are
    # only leads; GitHub's public organization endpoint does not establish that
    # the account is an employer or that a vacancy exists.
    try:
        from gharchive_live import fetch_hour, resolve_public_organization
        activity = fetch_hour(compressed_limit=8 * 1024 * 1024, expanded_limit=48 * 1024 * 1024, max_events=60_000)
        ranked = sorted(activity["leads"], key=lambda x: (x["hiring_signals"], sum(x["technologies"].values()), x["event_count"]), reverse=True)
        lookup_errors = 0
        non_organizations = 0
        for lead in ranked[:12]:
            try:
                org = resolve_public_organization(lead["organization"])
            except Exception:
                lookup_errors += 1
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
                                      "organization_lookups_attempted": min(12, len(ranked)),
                                      "organization_profiles_resolved": len(company_leads),
                                      "not_organization_or_not_found": non_organizations,
                                      "lookup_errors": lookup_errors,
                                      "employer_verified": 0, "vacancies_verified": 0}
    except Exception as exc:
        errors.append({"source": "gharchive_live", "error": type(exc).__name__})
        results["gharchive_live"] = {"error": type(exc).__name__}
    source_observations = len(records)
    unique = deduplicate(records)
    unique.sort(key=lambda x: (x["india_eligibility"] != "eligible_signal", x["relevance_score"] * -1, x["company"].casefold(), x["title"].casefold()))
    rejected = [row for row in unique if row["india_eligibility"] == "excluded"]
    opportunities = [row for row in unique if row["india_eligibility"] != "excluded"]
    def top20(rows, key):
        chosen = sorted(rows, key=key, reverse=True)[:20]
        return {"rows": len(chosen), "distinct_employer_names": len({x["company"].casefold() for x in chosen}),
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
                       "distinct_employer_names": len({x["company"].casefold() for x in unique}),
                       "provider_listings_observed": len(unique),
                       "official_employer_application_urls": sum(bool(x["direct_application_url"]) and x["official_employer_application_verified"] for x in unique),
                       "india_eligibility_signals": sum(x["india_eligibility"] == "eligible_signal" for x in unique),
                       "unknown_eligibility": sum(x["india_eligibility"] == "unknown" for x in unique),
                       "excluded": len(rejected),
                       "ranked_investigation_listings": len(opportunities),
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
            prior["also_seen_in"] = sorted(set(prior.get("also_seen_in", [prior["discovery_source"]])) | {row["discovery_source"]})
            if prior["india_eligibility"] != "eligible_signal" and row["india_eligibility"] == "eligible_signal":
                prior["india_eligibility"] = row["india_eligibility"]
                prior["eligibility_evidence"] = row["eligibility_evidence"]
            continue
        row["also_seen_in"] = [row["discovery_source"]]
        by_key[key] = row
        result.append(row)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default="reports/latest-discovery.json")
    parser.add_argument("--source", action="append", choices=list(SOURCES))
    parser.add_argument("--skill", action="append", default=[])
    args = parser.parse_args()
    skills = args.skill or ["Go", "Python", "TypeScript", "Node.js", "React", "Kubernetes", "PostgreSQL", "AWS"]
    report = discover(skills, args.source)
    target = Path(args.output)
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(target), "totals": report["totals"], "source_results": report["source_results"], "errors": report["errors"]}, indent=2))
    return int(bool(report["errors"]))


if __name__ == "__main__":
    raise SystemExit(main())
