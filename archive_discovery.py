"""Discover hiring evidence from public web snapshots, without guessing ATS slugs."""
import argparse
import json
import re
import hashlib
import time
from pathlib import Path
from collections import defaultdict
from urllib.parse import urlsplit, urlencode
from urllib.request import Request
from urllib.error import HTTPError, URLError
from network_guard import PublicHTTPSProxy, build_public_opener

CAREER = re.compile(r"(?:^|[/. _-])(careers?|jobs?|join-us|work-with-us|open-positions|vacancies)(?:$|[/. _-])", re.I)
def classify(url):
    p = urlsplit(url)
    if p.scheme not in ("https","http") or not p.hostname:
        return None
    host = p.hostname.lower()
    if CAREER.search(host) or CAREER.search(p.path):
        return host, url
    return None

def aggregate(records):
    companies = defaultdict(dict)
    for row in records:
        if str(row.get("status")) != "200":
            continue
        found = classify(row.get("url",""))
        if not found:
            continue
        host,url = found
        if url not in companies[host] or row.get("timestamp","") > companies[host][url].get("timestamp",""):
            companies[host][url] = {"url":url,"timestamp":row.get("timestamp",""),"kind":"historical_career_page"}
    return [{"host":h,"evidence":sorted(v.values(),key=lambda x:x["url"]),"live_verified":False} for h,v in sorted(companies.items())]

def get(url, *, cache_dir=None, retries=2, timeout=18):
    """Bound Common Crawl reads, cache immutable query results, and cool down 5xxs."""
    root = Path(cache_dir) if cache_dir else Path.home() / ".cache" / "job-intelligence" / "commoncrawl"
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    key = hashlib.sha256(url.encode()).hexdigest()
    cache_file = root / (key + ".json")
    if cache_file.exists():
        try:
            cached = json.loads(cache_file.read_text(encoding="utf-8"))
            if "body" in cached:
                return cached["body"]
            if cached.get("retry_at", 0) > time.time():
                raise RuntimeError("Common Crawl endpoint is in cached failure cooldown")
        except RuntimeError:
            raise
        except (OSError, ValueError, TypeError):
            cache_file.unlink(missing_ok=True)
    last_error = None
    for attempt in range(retries + 1):
        try:
            with PublicHTTPSProxy() as proxy:
                with build_public_opener(proxy.url).open(Request(url,headers={"User-Agent":"OpportunityResearch/0.1 (public archive research)"}),timeout=timeout) as r:
                    data=r.read(16*1024*1024+1)
                    if len(data)>16*1024*1024:
                        raise ValueError("archive response too large")
                    body = data.decode("utf-8")
            temp = cache_file.with_suffix(".tmp")
            temp.write_text(json.dumps({"body": body, "saved_at": time.time()}), encoding="utf-8")
            temp.replace(cache_file)
            return body
        except HTTPError as exc:
            last_error = exc
            if exc.code not in (502, 503, 504) or attempt >= retries:
                if exc.code in (502, 503, 504):
                    temp = cache_file.with_suffix(".tmp")
                    temp.write_text(json.dumps({"retry_at": time.time() + 900, "error": "HTTP " + str(exc.code)}), encoding="utf-8")
                    temp.replace(cache_file)
                raise
        except (TimeoutError, URLError) as exc:
            last_error = exc
            if attempt >= retries:
                temp = cache_file.with_suffix(".tmp")
                temp.write_text(json.dumps({"retry_at": time.time() + 900, "error": type(exc).__name__}), encoding="utf-8")
                temp.replace(cache_file)
                raise
        time.sleep(0.4 * (attempt + 1))
    raise last_error

def latest_index(cache_dir=None):
    """Read the current Common Crawl index registry instead of pinning a stale crawl."""
    rows=json.loads(get("https://index.commoncrawl.org/collinfo.json", cache_dir=cache_dir))
    indexes=[row.get("id","") for row in rows if re.fullmatch(r"CC-MAIN-\d{4}-\d{2}",row.get("id",""))]
    if not indexes:
        raise ValueError("Common Crawl did not return a recognized index")
    return indexes[0]

def archive(domain, index, *, cache_dir=None):
    if not re.fullmatch(r"[a-zA-Z0-9.-]+",domain) or not re.fullmatch(r"CC-MAIN-\d{4}-\d{2}",index):
        raise ValueError("invalid domain or index")
    q=urlencode({"url":domain,"matchType":"domain","output":"json","filter":"status:200","pageSize":1})
    raw=get("https://index.commoncrawl.org/"+index+"-index?"+q, cache_dir=cache_dir)
    return [json.loads(line) for line in raw.splitlines() if line.strip()]

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--domain",action="append",default=[])
    p.add_argument("--index",default="CC-MAIN-2026-43")
    p.add_argument("--jsonl",help="Local Common Crawl index export")
    args=p.parse_args()
    rows=[]
    errors=[]
    if args.jsonl:
        with open(args.jsonl,encoding="utf-8") as f:
            rows.extend(json.loads(line) for line in f if line.strip())
    for domain in args.domain:
        try:
            rows.extend(archive(domain,args.index))
        except Exception as exc:
            errors.append({"domain":domain,"error":str(exc)})
    print(json.dumps({"companies":aggregate(rows),"errors":errors},indent=2))
    return int(bool(errors))

if __name__=="__main__":
    raise SystemExit(main())
