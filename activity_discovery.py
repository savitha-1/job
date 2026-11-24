"""Turn bounded GH Archive exports into unverified engineering-employer leads."""
import gzip
import json
import re
from collections import Counter, defaultdict
from pathlib import Path

TECH = ("Go", "Python", "TypeScript", "JavaScript", "Kubernetes", "React", "Rust", "Java", "PostgreSQL", "AWS")
HIRING = re.compile(r"\b(hiring|we.re hiring|open role|job opening|join our team|careers?)\b", re.I)


def ingest_events(path, *, max_bytes=128 * 1024 * 1024, max_events=250_000, store=None):
    """Read local GH Archive JSONL(.gz), retaining only aggregate public signals."""
    path = Path(path)
    opener = gzip.open if path.suffix == ".gz" else open
    orgs = defaultdict(lambda: {"events": 0, "repositories": set(), "technologies": Counter(), "hiring_signals": 0, "evidence": []})
    read_bytes = events = 0
    with opener(path, "rb") as stream:
        while events < max_events:
            line = stream.readline(2 * 1024 * 1024 + 1)
            if not line:
                break
            read_bytes += len(line)
            if read_bytes > max_bytes:
                raise ValueError("GH Archive input exceeds byte budget")
            if len(line) > 2 * 1024 * 1024:
                continue
            try:
                row = json.loads(line)
            except (ValueError, UnicodeDecodeError):
                continue
            events += 1
            repo = row.get("repo") or {}
            owner = (row.get("org") or {}).get("login") or str(repo.get("name", "")).split("/", 1)[0]
            if not owner or not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", owner):
                continue
            item = orgs[owner.lower()]
            item["events"] += 1
            if repo.get("name"):
                item["repositories"].add(repo["name"])
            payload = " ".join(str(row.get(k, "")) for k in ("type", "created_at"))
            detail = row.get("payload") or {}
            payload += " " + " ".join(str(detail.get(k, ""))[:1000] for k in ("title", "body", "description"))
            payload += " " + " ".join(str(commit.get("message", ""))[:200] for commit in (detail.get("commits") or [])[:10] if isinstance(commit, dict))
            for tech in TECH:
                if re.search(r"(?<!\w)" + re.escape(tech) + r"(?!\w)", payload, re.I):
                    item["technologies"][tech] += 1
            if HIRING.search(payload):
                item["hiring_signals"] += 1
            url = "https://github.com/" + str(repo.get("name", owner)).strip("/")
            if len(item["evidence"]) < 5:
                item["evidence"].append({"url": url, "kind": "github_public_activity", "event_type": str(row.get("type", "unknown"))})
    leads = []
    for owner, item in sorted(orgs.items()):
        lead = {"organization": owner, "candidate_domain": "github.com", "verified_employer": False, "event_count": item["events"], "repositories": sorted(item["repositories"]), "technologies": dict(item["technologies"]), "hiring_signals": item["hiring_signals"], "evidence": item["evidence"]}
        leads.append(lead)
        if store:
            store.save_company_lead(lead, "gharchive")
    return {"events_read": events, "bytes_read": read_bytes, "leads": leads, "source": "local_gharchive_export"}
