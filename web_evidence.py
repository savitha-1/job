"""Public HTML hiring evidence: structured JobPosting data and career link extraction."""
import json
import re
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit
from archive_discovery import classify

class EvidenceParser(HTMLParser):
    def __init__(self, url):
        super().__init__(convert_charrefs=True)
        self.url=url
        self.links=set()
        self.jobs=[]
        self.script=False
        self.buffer=[]
    def handle_starttag(self, tag, attrs):
        a=dict(attrs)
        if tag=="a" and a.get("href"):
            u=urljoin(self.url,a["href"])
            if classify(u):
                self.links.add(u.split("#")[0])
        if tag=="script" and a.get("type","").lower()=="application/ld+json":
            self.script=True
            self.buffer=[]
    def handle_data(self,data):
        if self.script:
            self.buffer.append(data)
    def handle_endtag(self,tag):
        if tag=="script" and self.script:
            self.script=False
            try:
                self.extract(json.loads("".join(self.buffer)))
            except (ValueError,RecursionError):
                pass
    def extract(self,obj):
        if isinstance(obj,list):
            for item in obj:
                self.extract(item)
        elif isinstance(obj,dict):
            types=obj.get("@type",[])
            if isinstance(types,str):
                types=[types]
            if "JobPosting" in types and obj.get("title"):
                self.jobs.append({"title":obj["title"],"evidence_url":self.url,"application_url":obj.get("url"),"location":obj.get("jobLocation"),"applicant_location":obj.get("applicantLocationRequirements"),"remote":obj.get("jobLocationType"),"date_posted":obj.get("datePosted"),"valid_through":obj.get("validThrough"),"salary":obj.get("baseSalary"),"organization":obj.get("hiringOrganization"),"status":"unverified"})
            for key in ("@graph","mainEntity","itemListElement","item"):
                if key in obj:
                    self.extract(obj[key])

def inspect_html(url,html):
    parser=EvidenceParser(url)
    parser.feed(html)
    parser.close()
    return {"career_links":sorted(parser.links),"job_postings":parser.jobs}

if __name__=="__main__":
    import argparse
    p=argparse.ArgumentParser()
    p.add_argument("url")
    p.add_argument("html_file")
    args=p.parse_args()
    with open(args.html_file,encoding="utf-8") as f:
        print(json.dumps(inspect_html(args.url,f.read()),indent=2))
