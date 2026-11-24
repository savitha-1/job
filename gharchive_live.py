"""Fetch one bounded recent GH Archive hour and resolve public org profiles."""
import datetime as dt
import json
from urllib.error import HTTPError
from urllib.parse import quote, urlsplit
from urllib.request import Request
import time

from activity_discovery import ingest_stream
from network_guard import PublicHTTPSProxy, build_public_opener, public_url


class GitHubRateLimited(RuntimeError):
    def __init__(self, reset_at=None):
        self.reset_at = int(reset_at) if str(reset_at or "").isdigit() else None
        super().__init__("GitHub public API rate limit reached")


def latest_completed_hour(now=None, delay_hours=3):
    now = now or dt.datetime.now(dt.timezone.utc)
    return (now.astimezone(dt.timezone.utc) - dt.timedelta(hours=delay_hours)).replace(minute=0, second=0, microsecond=0)


def fetch_hour(hour=None, *, compressed_limit=24 * 1024 * 1024, expanded_limit=192 * 1024 * 1024, max_events=180_000):
    hour = hour or latest_completed_hour()
    stamp = hour.strftime("%Y-%m-%d-%-H") if hasattr(hour, "strftime") else ""
    # %-H is unavailable on some platforms; ensure the provider's zero-padded hour format.
    stamp = f"{hour:%Y-%m-%d}-{hour.hour}"
    url = f"https://data.gharchive.org/{stamp}.json.gz"
    request = Request(url, headers={"User-Agent": "LocalJobResearch/0.1 (bounded public engineering discovery)", "Accept": "application/gzip"})
    with PublicHTTPSProxy() as proxy:
        with build_public_opener(proxy.url).open(request, timeout=30) as response:
            length = response.headers.get("Content-Length")
            if length and int(length) > compressed_limit:
                raise ValueError("recent GH Archive hour exceeds compressed byte budget")
            compressed = response.read(compressed_limit + 1)
    if len(compressed) > compressed_limit:
        raise ValueError("recent GH Archive hour exceeds compressed byte budget")
    import gzip
    import io
    with gzip.GzipFile(fileobj=io.BytesIO(compressed)) as stream:
        aggregate = ingest_stream(stream, max_bytes=expanded_limit, max_events=max_events)
    aggregate.update({"hour_utc": hour.isoformat(), "source_url": url, "compressed_bytes": len(compressed)})
    return aggregate


def resolve_public_organization(login, *, token=None, timeout=15):
    """A GitHub name is a lead only; this requires a public Org API response."""
    if not isinstance(login, str) or not login or len(login) > 100 or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-" for c in login):
        raise ValueError("invalid organization login")
    url = "https://api.github.com/orgs/" + quote(login, safe="")
    headers = {"User-Agent": "LocalJobResearch/0.1", "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        headers["Authorization"] = "Bearer " + token
    request = Request(url, headers=headers)
    try:
        with PublicHTTPSProxy() as proxy:
            with build_public_opener(proxy.url).open(request, timeout=timeout) as response:
                remaining = response.headers.get("X-RateLimit-Remaining")
                reset = response.headers.get("X-RateLimit-Reset")
                if remaining == "0":
                    raise GitHubRateLimited(reset)
                body = response.read(512 * 1024 + 1)
    except HTTPError as exc:
        if exc.code in (404, 410):
            return None
        if exc.code in (403, 429) and (exc.headers.get("X-RateLimit-Remaining") == "0" or exc.headers.get("Retry-After")):
            reset = exc.headers.get("X-RateLimit-Reset")
            if not reset and exc.headers.get("Retry-After"):
                try:
                    reset = int(time.time()) + int(exc.headers["Retry-After"])
                except ValueError:
                    reset = None
            raise GitHubRateLimited(reset) from exc
        raise
    if len(body) > 512 * 1024:
        raise ValueError("GitHub organization profile exceeds response budget")
    payload = json.loads(body)
    if payload.get("type") != "Organization" or payload.get("login", "").casefold() != login.casefold():
        return None
    homepage = str(payload.get("blog") or "").strip()
    if homepage and "://" not in homepage:
        homepage = "https://" + homepage
    if not homepage or not public_url(homepage):
        homepage = None
    return {"login": payload.get("login"), "name": payload.get("name") or payload.get("login"),
            "description": str(payload.get("description") or "")[:1000], "homepage_claim": homepage,
            "location": str(payload.get("location") or "")[:200], "public_repos": int(payload.get("public_repos") or 0),
            "verified_github_organization": True, "verified_employer": False,
            "evidence_url": url, "observed_at": dt.datetime.now(dt.timezone.utc).isoformat()}
