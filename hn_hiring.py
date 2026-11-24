"""Bounded parser for the public Hacker News Who-is-hiring thread.

The thread is a discovery source, not proof of an employer-controlled vacancy.
Every resulting record remains unverified until corroborated on an official
employer or ATS page.
"""
import re
import datetime as dt
from html.parser import HTMLParser
from html import unescape
from urllib.parse import quote, urlsplit
from urllib.request import Request
from urllib.request import HTTPRedirectHandler
from urllib.error import HTTPError

from job_sources import _request_json, _iso_date
from network_guard import PublicHTTPSProxy, build_public_opener, origin, public_url
from web_crawler import _allowed_by_robots
from web_evidence import inspect_html

API = "https://hn.algolia.com/api/v1"


class _SameOriginRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if origin(req.full_url) != origin(newurl):
            raise HTTPError(req.full_url, code, "cross-origin redirect blocked", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class _CommentText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.urls = []

    def handle_starttag(self, tag, attrs):
        if tag in {"p", "br", "li", "div"}:
            self.parts.append("\n")
        if tag == "a":
            href = dict(attrs).get("href", "")
            if href:
                self.urls.append(unescape(href))

    def handle_data(self, data):
        self.parts.append(data)


def _plain(value):
    parser = _CommentText()
    parser.feed(str(value or ""))
    value = "".join(parser.parts)
    value = re.sub(r"[ \t\r\f\v]+", " ", value)
    value = re.sub(r" *\n+ *", "\n", value).strip()
    value = re.sub(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", "[email removed]", value, flags=re.I)
    value = re.sub(r"(?:\+?\d[\d ().-]{8,}\d)", "[phone removed]", value)
    return value


def parse_comment(hit, thread_id):
    text = _plain(hit.get("comment_text")).replace("\\n", "\n")
    if not text:
        return None
    # HN hiring posts usually put company/location/work-mode in the first
    # delimited line. Keep the raw header as evidence; don't invent fields.
    header = re.split(r"\n+", text, maxsplit=1)[0].strip()
    fields = [part.strip(" |\t-") for part in re.split(r"\s*[|•]\s*", header) if part.strip(" |\t-")]
    if not fields:
        return None
    company = fields[0][:160]
    if len(company) < 2 or company.casefold() in {"ask hn", "deleted", "removed"} or re.search(r"\b(seeking freelance|seeking work|looking for work|available for hire|portfolio:|resume:|cv:)\b", company, re.I):
        return None
    role_pattern = re.compile(r"\b(?:software|backend|back-end|frontend|front-end|full[- ]stack|platform|infrastructure|site reliability|data|security|systems|devops|machine learning|ml|ai|product|founding|staff|senior|principal|engineering)\s+(?:engineer|developer|programmer|architect|scientist|specialist|sre)\b|\b(?:engineer|developer|sre)\b", re.I)
    title_index = next((i for i, field in enumerate(fields) if role_pattern.search(field)), None)
    title_match = role_pattern.search(fields[title_index]) if title_index is not None else None
    title = title_match.group(0) if title_match else ""
    if not title:
        return None
    raw = str(hit.get("comment_text") or "")
    parser = _CommentText()
    parser.feed(raw)
    candidates = list(parser.urls) + re.findall(r"https?://[^\s<>\]\[()\"']+", raw)
    urls = []
    for candidate in candidates:
        candidate = unescape(candidate).rstrip(".,;!?")
        if public_url(candidate) and urlsplit(candidate).hostname not in {"news.ycombinator.com", "hn.algolia.com"}:
            urls.append(candidate)
    def link_rank(candidate):
        parsed = urlsplit(candidate)
        host = (parsed.hostname or "").casefold()
        path = parsed.path.casefold()
        ats = ("greenhouse.io", "lever.co", "ashbyhq.com", "workable.com", "jobvite.com", "icims.com", "smartrecruiters.com")
        return (10 if any(host == suffix or host.endswith("." + suffix) for suffix in ats) else 0) + (5 if re.search(r"/jobs?/|career|apply|opening", path) else 0)
    urls = sorted(dict.fromkeys(urls), key=lambda u: (-link_rank(u), u))
    comment_id = str(hit.get("objectID", ""))
    if not comment_id:
        return None
    return {"source": "hn_whoishiring", "source_name": "Hacker News Who is Hiring", "source_credit": "Hacker News / Algolia", "source_home": "https://news.ycombinator.com/", "source_id": comment_id,
            "employer": company, "title": fields[title_index][:200], "location": " | ".join(fields[title_index + 1:])[:300] or "Unknown", "description": text[:12000],
            "remote": None, "published_at": _iso_date(hit.get("created_at")), "observed_at": hit.get("created_at") or "",
            "source_url": f"https://news.ycombinator.com/item?id={comment_id}", "official_url": None,
            "candidate_urls": urls[:10], "thread_url": f"https://news.ycombinator.com/item?id={thread_id}", "tags": [], "job_type": "Unknown", "seniority": "Unknown",
            "vacancy_verified": False, "official_url_verified": False, "evidence": {"header": header[:500], "source_comment_id": comment_id}}


def fetch_latest(*, max_comments=1200):
    if not 1 <= max_comments <= 1500:
        raise ValueError("comment bound must be between 1 and 1500")
    story = _request_json(API + "/search_by_date?tags=story,author_whoishiring&query=" + quote('Ask HN: Who is hiring?', safe="") + "&hitsPerPage=10", limit=2_000_000)
    hits = [hit for hit in story.get("hits", []) if hit.get("title", "").casefold().startswith("ask hn: who is hiring")]
    if not hits:
        return {"thread": None, "comments_read": 0, "opportunities": []}
    thread = hits[0]
    thread_id = str(thread["objectID"])
    comments = []
    page = 0
    while len(comments) < max_comments:
        limit = min(1000, max_comments - len(comments))
        payload = _request_json(f"{API}/search_by_date?tags=comment,story_{thread_id}&hitsPerPage={limit}&page={page}", limit=16 * 1024 * 1024)
        batch = payload.get("hits", [])
        comments.extend(batch)
        if not batch or page + 1 >= int(payload.get("nbPages", 0)):
            break
        page += 1
    # The Algolia story tag includes nested discussion; only direct children
    # are the employer/job-seeker submissions in the monthly hiring thread.
    employer_posts = [hit for hit in comments if str(hit.get("parent_id")) == thread_id]
    rows = [row for row in (parse_comment(hit, thread_id) for hit in employer_posts) if row]
    return {"thread": {"id": thread_id, "title": thread.get("title"), "created_at": thread.get("created_at"), "url": f"https://news.ycombinator.com/item?id={thread_id}"}, "comments_read": len(comments), "top_level_comments": len(employer_posts), "opportunities": rows}


def verify_candidate(row, *, timeout=5, max_bytes=3 * 1024 * 1024, robots_cache=None):
    """Compatibility API backed by the shared multi-strategy verifier."""
    from vacancy_verifier import verify_url
    cache = robots_cache if robots_cache is not None else {}
    failures = []
    target = {**row, "source": row.get("source", "hn_whoishiring"),
              "source_id": row.get("source_id", row.get("id", "legacy"))}
    for url in row.get("candidate_urls", []):
        result = verify_url(target, url, timeout=timeout, max_bytes=max_bytes, robots_cache=cache)
        if result.get("verified"):
            evidence = result["evidence"]
            return {**result, "employer": evidence["employer"], "title": evidence["title"],
                    "vacancy_url": evidence["vacancy_url"], "application_url": evidence["application_url"],
                    "evidence_url": evidence["evidence_url"], "india_eligibility": evidence["india_eligibility"]}
        failures.append({"url": url, "reason": result.get("reason"), "method": result.get("method"),
                         "retryable": result.get("retryable", False), "alternatives": result.get("alternatives", [])})
    return {"verified": False, "reason": "no_candidate_link_verified", "failures": failures}


def _norm(value):
    return " ".join(re.findall(r"[a-z0-9]+", str(value or "").casefold()))
