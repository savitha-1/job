"""Bounded, attributed integrations for public international job feeds.

These feeds are discovery leads. Their job-board links are not mislabeled as
employer application URLs; official application URLs remain unknown unless a
provider supplies an ATS URL or a later permitted verification confirms one.
"""
import datetime as dt
import json
import re
from html import unescape
from urllib.parse import urlencode, urlsplit
from urllib.request import Request

from network_guard import PublicHTTPSProxy, build_public_opener
from network_guard import public_url

SOURCES = {
    "arbeitnow_de": {"name": "Arbeitnow", "url": "https://www.arbeitnow.com/", "credit": "Arbeitnow", "endpoint": "https://www.arbeitnow.com/api/job-board-api"},
    "arbeitnow_uk": {"name": "Arbeitnow UK", "url": "https://www.arbeitnow.co.uk/", "credit": "Arbeitnow", "endpoint": "https://www.arbeitnow.co.uk/api/job-board-api"},
    "remoteok": {"name": "Remote OK", "url": "https://remoteok.com/", "credit": "Remote OK", "endpoint": "https://remoteok.com/api"},
    "jobicy": {"name": "Jobicy", "url": "https://jobicy.com/", "credit": "Jobicy", "endpoint": "https://jobicy.com/api/jobs"},
}


def _request_json(url, *, limit=16 * 1024 * 1024, timeout=20):
    request = Request(url, headers={"User-Agent": "LocalJobResearch/0.1 (single-user; attributed public job discovery)", "Accept": "application/json"})
    with PublicHTTPSProxy() as proxy:
        with build_public_opener(proxy.url).open(request, timeout=timeout) as response:
            body = response.read(limit + 1)
    if len(body) > limit:
        raise ValueError("job feed response exceeds size limit")
    return json.loads(body)


def _text(value):
    value = unescape(re.sub(r"(?is)<(script|style)\b.*?</\1\s*>", " ", str(value or "")))
    value = re.sub(r"(?s)<[^>]+>", " ", value)
    value = re.sub(r"[\t\r\n ]+", " ", value).strip()
    value = re.sub(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", "[email removed]", value, flags=re.I)
    return value[:50000]


def _iso_date(value):
    if not value:
        return None
    if isinstance(value, (int, float)):
        try:
            return dt.datetime.fromtimestamp(value, dt.timezone.utc).isoformat()
        except (ValueError, OSError, OverflowError):
            return None
    try:
        return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(dt.timezone.utc).isoformat()
    except (ValueError, TypeError):
        return None


def normalize(source, payload):
    observed = dt.datetime.now(dt.timezone.utc).isoformat()
    if source in ("arbeitnow_de", "arbeitnow_uk"):
        jobs = payload.get("data", [])
        rows = [{"id": j.get("slug") or j.get("url"), "title": j.get("title"), "company": j.get("company_name"),
                 "location": j.get("location"), "description": j.get("description"), "remote": j.get("remote"),
                 "published": j.get("created_at"), "url": j.get("url"), "official_url": None,
                 "tags": j.get("tags", []), "job_type": j.get("job_types", [])} for j in jobs]
    elif source == "remoteok":
        rows = []
        for job in payload if isinstance(payload, list) else []:
            if not isinstance(job, dict) or not job.get("id") or not job.get("position"):
                continue  # first element is feed metadata/legal notice
            rows.append({"id": str(job["id"]), "title": job.get("position"), "company": job.get("company"),
                         "location": job.get("location") or "Remote (location unspecified)",
                         "description": job.get("description"), "remote": True,
                         "published": job.get("date") or job.get("epoch"), "url": job.get("url"),
                         "official_url": job.get("apply_url"), "tags": job.get("tags", []),
                         "job_type": job.get("employment_type")})
    elif source == "jobicy":
        rows = [{"id": str(j.get("id") or j.get("jobSlug")), "title": j.get("jobTitle"), "company": j.get("companyName"),
                 "location": j.get("jobGeo"), "description": j.get("jobDescription") or j.get("jobExcerpt"),
                 "remote": True, "published": j.get("pubDate"), "url": j.get("url"), "official_url": None,
                 "tags": j.get("jobIndustry", []), "job_type": j.get("jobType", []), "seniority": j.get("jobLevel")}
                for j in payload.get("jobs", [])]
    else:
        raise ValueError("unknown public feed")

    result = []
    source_meta = SOURCES[source]
    for row in rows:
        if not row.get("title") or not row.get("company") or not row.get("url"):
            continue
        if not public_url(str(row["url"])):
            continue
        official_url = row.get("official_url")
        if not official_url or not public_url(str(official_url)):
            official_url = None
        elif source == "remoteok" and urlsplit(str(official_url)).hostname.casefold().removeprefix("www.") == "remoteok.com":
            # This is the Remote OK listing page, not the employer's application form.
            official_url = None
        published = _iso_date(row.get("published"))
        result.append({
            "source": source, "source_name": source_meta["name"], "source_credit": source_meta["credit"],
            "source_home": source_meta["url"], "source_id": str(row["id"]),
            "employer": _text(row["company"])[:200], "title": _text(row["title"])[:300],
            "location": _text(row.get("location"))[:300], "description": _text(row.get("description")),
            "remote": bool(row.get("remote")), "published_at": published, "observed_at": observed,
            "source_url": row["url"], "official_url": official_url,
            "tags": [_text(x) for x in (row.get("tags") or [])[:30]],
            "job_type": _text(row.get("job_type")), "seniority": _text(row.get("seniority")),
            "freshness": "unknown" if not published else "feed_current",
            # Presence in an aggregator feed is a source observation, not
            # employer verification. Keep both stronger flags false.
            "vacancy_verified": False, "official_url_verified": False,
        })
    return result


def fetch(source, *, page=1, count=100):
    if source not in SOURCES:
        raise ValueError("unknown source")
    if page < 1 or not 1 <= count <= 200:
        raise ValueError("source bounds exceeded")
    endpoint = SOURCES[source]["endpoint"]
    if source.startswith("arbeitnow"):
        endpoint += "?" + urlencode({"page": page})
    elif source == "jobicy":
        endpoint += "?" + urlencode({"count": count, "industry": "engineering"})
    payload = _request_json(endpoint)
    return normalize(source, payload)


def freshness(record, *, now=None, max_age_days=30):
    if not record.get("published_at"):
        return "unknown"
    try:
        posted = dt.datetime.fromisoformat(record["published_at"])
        age = ((now or dt.datetime.now(dt.timezone.utc)) - posted).total_seconds()
    except (ValueError, TypeError):
        return "unknown"
    return "fresh" if -3600 <= age <= max_age_days * 86400 else "stale"
