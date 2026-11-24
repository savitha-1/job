"""Shared persistent verification worker used by CLI and dashboard discovery."""
import datetime as dt
import re
import time
from pathlib import Path

from verification_frontier import VerificationFrontier
from vacancy_verifier import verify_url


def run_verification_batch(leads, state_path, skills, *, max_urls=60, max_requests=240, browser_fallback=True):
    started=time.monotonic()
    frontier=VerificationFrontier(state_path)
    recovered=frontier.recover_stale()
    for lead in leads:
        payload=dict(lead)
        payload["skills"]=list(skills)
        frontier.enqueue_candidate(payload,priority=float(lead.get("relevance_score",0))+ (15 if lead.get("india_eligibility")=="eligible_signal" else 0))
    work=frontier.claim_urls(max_urls)
    robots_cache={}
    requests_used=bytes_read=0
    verified=[]
    failure_counts={}
    processed=0
    for index,item in enumerate(work):
        if requests_used>=max_requests:
            for pending in work[index:]:
                frontier.release_claim(pending)
            break
        target={"source":item["source"],"source_id":item["source_id"],"employer":item["employer"],"title":item["title"],**item["payload"]}
        try:
            result=verify_url(target,item["url"],robots_cache=robots_cache,browser_fallback=browser_fallback,
                              request_budget=max_requests-requests_used)
        except Exception as exc:
            result={"verified":False,"outcome":"worker_error","reason":"worker_"+type(exc).__name__,
                    "method":"verification_worker","retryable":False,"request_count":0,"bytes_read":0,
                    "alternatives":["retry by launching a subsequent durable batch"]}
        for link in result.get("discovered_urls",[]):
            frontier.enqueue_discovered(item["target_id"],[link],stage=2)
        frontier.finish_url(item,result)
        processed+=1
        requests_used+=int(result.get("request_count",0))
        bytes_read+=int(result.get("bytes_read",0))
        if result.get("verified"):
            lead=item["payload"]
            evidence=result["evidence"]
            verified.append({"company":evidence["employer"],"official_website":None,"title":evidence["title"],
                "direct_application_url":evidence["application_url"],"source_listing_url":evidence["vacancy_url"],
                "work_location":evidence["location"],"india_eligibility":evidence["india_eligibility"],
                "eligibility_evidence":evidence["eligibility_evidence"],"technical_matches":evidence["technical_relevance"]["matches"],
                "relevance_score":evidence["technical_relevance"]["score"],
                "keyword_baseline_score":sum(bool(re.search(r"(?<!\w)"+re.escape(str(skill))+r"(?!\w)",(evidence["title"]+" "+evidence.get("description", "")),re.I)) for skill in skills),
                "compensation":"Unknown",
                "published_at":lead.get("published_at"),"observed_at":evidence["observed_at"],
                "discovery_source":lead.get("source_name",lead.get("source","unknown")),
                "source_credit":lead.get("source_credit",""),"source_status":"verified against a current public vacancy source",
                "official_employer_application_verified":True,"vacancy_verified":True,
                "vacancy_evidence_url":evidence["evidence_url"],"vacancy_evidence":evidence,
                "requisition_id":evidence["requisition_id"],"lead_source_url":lead.get("source_url")})
        else:
            reason=result.get("reason","not_verified")
            failure_counts[reason]=failure_counts.get(reason,0)+1
    stats=frontier.stats()
    return {"verified_opportunities":verified,"processed_urls":processed,"requested_urls":len(work),
        "requests_used":requests_used,"bytes_read":bytes_read,"elapsed_seconds":round(time.monotonic()-started,3),
        "recovered_after_crash":recovered,"failure_reasons":failure_counts,"frontier":stats,
        "state_path":str(Path(state_path).expanduser()),"observed_at":dt.datetime.now(dt.timezone.utc).isoformat()}
