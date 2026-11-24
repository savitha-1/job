"""Discover hiring evidence from public web snapshots, without guessing ATS slugs."""
import argparse
import json
import re
from collections import defaultdict
from urllib.parse import urlsplit, urlencode
from urllib.request import Request, urlopen

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

def get(url):
    with urlopen(Request(url,headers={"User-Agent":"OpportunityResearch/0.1 (public archive research)"}),timeout=30) as r:
        data=r.read(16*1024*1024+1)
        if len(data)>16*1024*1024:
            raise ValueError("archive response too large")
        return data.decode("utf-8")

def archive(domain, index):
    if not re.fullmatch(r"[a-zA-Z0-9.-]+",domain) or not re.fullmatch(r"CC-MAIN-\d{4}-\d{2}",index):
        raise ValueError("invalid domain or index")
    q=urlencode({"url":domain,"matchType":"domain","output":"json","filter":"status:200","pageSize":1})
    raw=get("https://index.commoncrawl.org/"+index+"-index?"+q)
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
