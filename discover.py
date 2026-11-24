"""Public ATS discovery, bounded concurrent reads, and evidence-preserving output."""
import argparse
import concurrent.futures
import datetime
import html
import json
import re
import sys
import urllib.request
from urllib.parse import urlsplit, quote
from urllib.error import HTTPError
import xml.etree.ElementTree as ET
from engine import rank
from network_guard import PublicHTTPSProxy, build_public_opener, public_url
from job_sources import _request_json

ENDPOINTS = {
    "greenhouse": "https://boards-api.greenhouse.io/v1/boards/{}/jobs?content=true",
    "lever": "https://api.lever.co/v0/postings/{}?mode=json",
    "lever-eu": "https://api.eu.lever.co/v0/postings/{}?mode=json",
    "ashby": "https://api.ashbyhq.com/posting-api/job-board/{}?includeCompensation=true",
}


def detect_public_ats(url):
    """Identify a public ATS board from an observed URL, never by guessing a slug."""
    if not public_url(url):
        return None
    p = urlsplit(url)
    host = (p.hostname or "").casefold()
    parts = [x for x in p.path.split("/") if x]
    if host in {"boards.greenhouse.io", "job-boards.greenhouse.io"} and parts:
        if parts[0].casefold() == "jobs" and len(parts) > 1:
            return {"source": "greenhouse", "board": parts[1], "job_id": parts[3] if len(parts) > 3 and parts[2] == "jobs" else None}
        return {"source": "greenhouse", "board": parts[0], "job_id": parts[2] if len(parts) > 2 and parts[1] == "jobs" else None}
    if host in {"jobs.lever.co", "jobs.eu.lever.co"} and parts:
        return {"source": "lever-eu" if host == "jobs.eu.lever.co" else "lever", "board": parts[0], "job_id": parts[1] if len(parts) > 1 else None}
    if host == "jobs.ashbyhq.com" and parts:
        return {"source": "ashby", "board": parts[0], "job_id": parts[1] if len(parts) > 1 else None}
    if host in {"careers.smartrecruiters.com", "jobs.smartrecruiters.com"} and parts:
        route = parts[1:] if len(parts) > 1 else []
        job_id = route[-1] if route and route[-1].casefold() not in {"job", "jobs", "postings"} else None
        return {"source": "smartrecruiters", "board": parts[0], "job_id": job_id}
    if host == "apply.workable.com" and parts:
        return {"source": "workable", "board": parts[0], "job_id": parts[2] if len(parts) > 2 and parts[1] == "j" else None}
    if host.endswith(".jobs.personio.de"):
        return {"source": "personio", "board": host.removesuffix(".jobs.personio.de"), "job_id": parts[-1] if parts else None}
    if host.endswith(".recruitee.com"):
        return {"source": "recruitee", "board": host.split(".")[0], "job_id": parts[-1] if parts else None}
    if host.endswith(".teamtailor.com"):
        return {"source": "teamtailor", "board": host.split(".")[0], "job_id": parts[-1] if parts else None}
    if host.endswith(".bamboohr.com") and "career" in p.path.casefold():
        return {"source": "bamboohr", "board": host.split(".")[0], "job_id": parts[-1] if len(parts) > 1 else None}
    return None


def fetch_public_ats(url, *, timeout=15):
    """Read public ATS listings using documented/public GET interfaces only."""
    detected = detect_public_ats(url)
    if not detected:
        return None
    source, board, job_id = detected["source"], detected["board"], detected["job_id"]
    if source in ENDPOINTS:
        jobs = fetch(source, board)
        jobs = [j for j in jobs if not job_id or str(j["id"]) == str(job_id) or str(j["application_url"]).rstrip("/").endswith("/" + str(job_id))]
        return {**detected, "jobs": jobs, "request_count": 1, "evidence_url": url, "method": "public_ats_json"}
    if source == "smartrecruiters":
        payload = _request_json(f"https://api.smartrecruiters.com/v1/companies/{quote(board, safe='')}/postings?limit=100", limit=8*1024*1024, timeout=timeout)
        content = payload.get("content", [])
        if job_id:
            content = [j for j in content if str(j.get("id")) == job_id or str(j.get("uuid")) == job_id]
        jobs = []
        # Listing endpoint is enough to shortlist. Fetch at most one detail,
        # and only when the observed URL identifies a specific posting.
        selected = content[:1] if job_id else []
        for item in selected:
            posting_id = str(item.get("id") or item.get("uuid") or "")
            details = _request_json(f"https://api.smartrecruiters.com/v1/companies/{quote(board, safe='')}/postings/{quote(posting_id, safe='')}", limit=4*1024*1024, timeout=timeout) if posting_id else item
            name = details.get("name") or item.get("name") or item.get("title") or ""
            company = (details.get("company") or {}).get("name") or ""
            apply = details.get("applyUrl") or details.get("apply_url") or ""
            jobs.append({"source":"smartrecruiters", "board":board, "id":posting_id, "title":name,
                "company":company, "location":_smart_location(details.get("location") or item.get("location")),
                "description":_smart_description(details), "application_url":apply or None,
                "evidence_url":f"https://api.smartrecruiters.com/v1/companies/{quote(board, safe='')}/postings/{quote(posting_id, safe='')}",
                "date_posted":details.get("releasedDate") or details.get("createdOn"), "active":True,
                "experience":(details.get("experienceLevel") or {}).get("label")})
        return {**detected, "jobs":jobs, "request_count":1 + int(bool(job_id and selected)), "evidence_url":url, "method":"smartrecruiters_public_posting_api"}
    if source == "workable":
        payload = _request_json(f"https://www.workable.com/api/accounts/{quote(board,safe='')}?details=true", limit=8*1024*1024, timeout=timeout)
        raw = payload.get("jobs", []) if isinstance(payload, dict) else []
        jobs=[]
        for item in raw:
            if str(item.get("state", "published")).casefold() != "published":
                continue
            code=str(item.get("shortcode") or item.get("id") or "")
            if job_id and code.casefold()!=job_id.casefold() and not str(item.get("url","")).rstrip("/").endswith("/"+job_id):
                continue
            loc=item.get("location") or {}
            jobs.append({"source":"workable","board":board,"id":code,"title":item.get("title") or item.get("full_title"),
                "company":payload.get("name") or board,"location":loc.get("location_str") or loc.get("country") or "",
                "description":item.get("description") or item.get("description_text") or "",
                "application_url":item.get("application_url") or item.get("shortlink") or item.get("url"),
                "evidence_url":item.get("url") or url,"date_posted":item.get("created_at"),"active":True})
        return {**detected,"jobs":jobs,"request_count":1,"evidence_url":url,"method":"workable_public_careers_endpoint"}
    if source == "personio":
        xml_url=f"https://{board}.jobs.personio.de/xml"
        request=urllib.request.Request(xml_url,headers={"User-Agent":"OpportunityResearch/0.1 (public career feed GET)","Accept":"application/xml,text/xml"})
        with PublicHTTPSProxy() as proxy:
            with build_public_opener(proxy.url).open(request,timeout=timeout) as response:
                body=response.read(8*1024*1024+1)
        if len(body)>8*1024*1024: raise ValueError("Personio feed response exceeds size limit")
        root=ET.fromstring(body); jobs=[]
        for item in root.findall(".//position"):
            jid=item.findtext("id") or ""
            if job_id and jid!=job_id: continue
            description=" ".join(x.text or "" for x in item.findall("./jobDescriptions/jobDescription/value"))
            jobs.append({"source":"personio","board":board,"id":jid,"title":item.findtext("name") or "",
                "company":board,"location":" | ".join(x for x in (item.findtext("office"),item.findtext("subcompany")) if x),
                "description":description,"application_url":None,"evidence_url":xml_url,"date_posted":item.findtext("createdAt"),"active":True,
                "seniority":item.findtext("seniority"),"years_experience":item.findtext("yearsOfExperience")})
        return {**detected,"jobs":jobs,"request_count":1,"evidence_url":xml_url,"method":"personio_public_xml_feed"}
    # Recruitee/Teamtailor APIs require employer-issued tokens. BambooHR does
    # not expose a documented unauthenticated postings API. Their public pages
    # are processed through the visible/structured HTML and browser strategies.
    return {**detected,"jobs":[],"request_count":0,"evidence_url":url,"method":"public_html_only","reason":"public_authenticated_api_not_available"}


def _smart_location(value):
    if isinstance(value, dict):
        return ", ".join(str(value.get(k)) for k in ("city","region","country") if value.get(k))
    return str(value or "")


def _smart_description(item):
    parts=[]
    if item.get("jobAd"):
        parts.append(str(item["jobAd"]))
    if item.get("jobAdSections"):
        for section in item["jobAdSections"]:
            parts.extend(str(section.get(k,"")) for k in ("title","text") if section.get(k))
    if item.get("jobAdContent"):
        parts.append(str(item["jobAdContent"]))
    return clean(" ".join(parts))
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
