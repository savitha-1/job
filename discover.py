"""Public ATS discovery, bounded concurrent reads, and evidence-preserving output."""
import argparse
import concurrent.futures
import datetime
import html
import json
import re
import sys
import urllib.request
from urllib.parse import urlsplit
from engine import rank
from network_guard import PublicHTTPSProxy, build_public_opener

ENDPOINTS = {
    "greenhouse": "https://boards-api.greenhouse.io/v1/boards/{}/jobs?content=true",
    "lever": "https://api.lever.co/v0/postings/{}?mode=json",
    "lever-eu": "https://api.eu.lever.co/v0/postings/{}?mode=json",
    "ashby": "https://api.ashbyhq.com/posting-api/job-board/{}?includeCompensation=true",
}
def endpoint(source, board):
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", board):
        raise ValueError("invalid board slug")
    return ENDPOINTS[source].format(board)

def clean(text):
    return html.unescape(re.sub(r"<[^>]+>", " ", str(text or "")))

def redact_contacts(text):
    text = re.sub(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", "[email redacted]", str(text), flags=re.I)
    return re.sub(r"(?<!\w)(?:\+?\d[\d ()-]{7,}\d)(?!\w)", "[phone redacted]", text)

def normalize(source, board, data):
    rows = data if source.startswith("lever") else data["jobs"]
    result = []
    for j in rows:
        if source == "ashby" and j.get("isListed") is False:
            continue
        if source == "greenhouse":
            title, location, url, desc = j.get("title"), (j.get("location") or {}).get("name"), j.get("absolute_url"), j.get("content")
        elif source.startswith("lever"):
            title, location, url, desc = j.get("text"), (j.get("categories") or {}).get("location"), j.get("applyUrl") or j.get("hostedUrl"), j.get("descriptionPlain")
        else:
            title, location, url, desc = j.get("title"), j.get("location"), j.get("applyUrl") or j.get("jobUrl"), j.get("descriptionPlain")
        if not title or not url or urlsplit(url).scheme != "https" or not urlsplit(url).hostname:
            continue
        result.append(dict(source=source, board=board, id=j.get("id") or url, title=title, location=location or "", description=redact_contacts(clean(desc)), application_url=url, evidence_url=endpoint(source,board), observed_at=datetime.datetime.now(datetime.timezone.utc).isoformat(), published_at=j.get("publishedAt"), compensation=j.get("compensation") or j.get("salaryRange")))
    return result

def fetch(source, board):
    url = endpoint(source, board)
    req = urllib.request.Request(url, headers={"User-Agent":"OpportunityResearch/0.1", "Accept":"application/json"})
    with PublicHTTPSProxy() as proxy:
        with build_public_opener(proxy.url).open(req, timeout=20) as response:
            body = response.read(32*1024*1024+1)
            if len(body)>32*1024*1024:
                raise ValueError("response exceeds size limit")
            return normalize(source,board,json.loads(body))

def discover(boards, workers=4):
    jobs, errors = [], []
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(fetch,*b): b for b in set(boards)}
        for future in concurrent.futures.as_completed(futures):
            try:
                jobs.extend(future.result())
            except Exception as exc:
                errors.append({"board":futures[future],"error":str(exc)})
    return jobs, errors

def main():
    p=argparse.ArgumentParser()
    p.add_argument("boards", nargs="+", help="source:board")
    p.add_argument("--output", default="opportunities.json")
    args=p.parse_args()
    boards=[tuple(b.split(":",1)) for b in args.boards]
    for b in boards:
        endpoint(*b)
    jobs,errors=discover(boards)
    ranked=rank(jobs,["Go","Python","TypeScript","Node.js","React","Kubernetes"])
    ranked=[j for j in ranked if j["skill_matches"]>0]
    with open(args.output,"w",encoding="utf-8") as f:
        json.dump({"observed":len(jobs),"errors":errors,"opportunities":ranked},f,indent=2,ensure_ascii=False)
    print(f"Observed {len(jobs)} jobs, ranked {len(ranked)}, failed boards {len(errors)}",file=sys.stderr)
    return 1 if errors else 0

if __name__=="__main__":
    sys.exit(main())
