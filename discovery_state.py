"""Local durable company/job evidence store and bounded prioritized frontier."""
import json
import sqlite3
import time
from contextlib import closing
from urllib.parse import urlsplit


class DiscoveryStore:
    def __init__(self, path):
        self.path = str(path)
        with closing(self.connect()) as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS employers (id TEXT PRIMARY KEY, name TEXT, domain TEXT UNIQUE, updated REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, employer_id TEXT NOT NULL, source TEXT NOT NULL, payload TEXT NOT NULL, observed REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS job_snapshots (id TEXT NOT NULL, observed REAL NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(id,observed));
                CREATE TABLE IF NOT EXISTS evidence (id INTEGER PRIMARY KEY, subject TEXT NOT NULL, url TEXT NOT NULL, kind TEXT NOT NULL, observed REAL NOT NULL, details TEXT NOT NULL, UNIQUE(subject,url,kind));
                CREATE TABLE IF NOT EXISTS frontier (url TEXT PRIMARY KEY, host TEXT NOT NULL, priority REAL NOT NULL, state TEXT NOT NULL DEFAULT 'queued', attempts INTEGER NOT NULL DEFAULT 0, updated REAL NOT NULL, depth INTEGER NOT NULL DEFAULT 0, parent TEXT);
                CREATE TABLE IF NOT EXISTS host_budget (host TEXT PRIMARY KEY, count INTEGER NOT NULL, window REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS checkpoints (name TEXT PRIMARY KEY, value TEXT NOT NULL, updated REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS crawl_pages (url TEXT NOT NULL, fingerprint TEXT NOT NULL, status INTEGER, title TEXT, observed REAL NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(url,fingerprint));
                CREATE TABLE IF NOT EXISTS crawl_edges (source TEXT NOT NULL, target TEXT NOT NULL, kind TEXT NOT NULL, observed REAL NOT NULL, PRIMARY KEY(source,target,kind));
                CREATE TABLE IF NOT EXISTS company_leads (id TEXT PRIMARY KEY, organization TEXT NOT NULL, source TEXT NOT NULL, verified INTEGER NOT NULL DEFAULT 0, observed REAL NOT NULL, payload TEXT NOT NULL);
            """)
            columns = {row[1] for row in db.execute("PRAGMA table_info(frontier)")}
            if "depth" not in columns:
                db.execute("ALTER TABLE frontier ADD COLUMN depth INTEGER NOT NULL DEFAULT 0")
            if "parent" not in columns:
                db.execute("ALTER TABLE frontier ADD COLUMN parent TEXT")

    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.execute("PRAGMA foreign_keys=ON")
        return db

    def add_employer(self, domain, name=None):
        host = (urlsplit(domain if "://" in domain else "https://" + domain).hostname or "").lower().rstrip(".")
        if not host or "." not in host:
            raise ValueError("valid employer domain required")
        with closing(self.connect()) as db, db:
            db.execute("INSERT INTO employers(id,name,domain,updated) VALUES(?,?,?,?) ON CONFLICT(domain) DO UPDATE SET name=COALESCE(excluded.name,employers.name),updated=excluded.updated", (host, name, host, time.time()))
            return host

    def save_job(self, employer_id, job):
        key = "\x1f".join(str(job.get(k, "")) for k in ("source", "board", "id"))
        payload = json.dumps(job, sort_keys=True)
        with closing(self.connect()) as db, db:
            previous = db.execute("SELECT max(observed) FROM job_snapshots WHERE id=?", (key,)).fetchone()[0]
            observed = max(time.time(), (previous + 0.001) if previous else 0)
            db.execute("INSERT INTO jobs VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload,observed=excluded.observed", (key, employer_id, str(job.get("source", "unknown")), payload, observed))
            db.execute("INSERT OR IGNORE INTO job_snapshots VALUES(?,?,?)", (key, observed, payload))
        return key

    def job_history(self, job_id):
        with closing(self.connect()) as db:
            return [json.loads(row[0]) for row in db.execute("SELECT payload FROM job_snapshots WHERE id=? ORDER BY observed", (job_id,))]

    def add_evidence(self, subject, url, kind, details=None):
        if urlsplit(url).scheme not in ("http", "https") or not urlsplit(url).hostname:
            raise ValueError("evidence URL must be public HTTP(S)")
        with closing(self.connect()) as db, db:
            db.execute("INSERT OR IGNORE INTO evidence(subject,url,kind,observed,details) VALUES(?,?,?,?,?)", (subject, url, kind, time.time(), json.dumps(details or {}, sort_keys=True)))

    def enqueue(self, url, priority=0, depth=0, parent=None, allow_local_fixture=False):
        p = urlsplit(url)
        fixture_http = allow_local_fixture and p.scheme == "http" and p.hostname in ("localhost", "127.0.0.1", "::1")
        if (p.scheme != "https" and not fixture_http) or not p.hostname or p.username or p.password:
            raise ValueError("frontier accepts HTTPS URLs only")
        with closing(self.connect()) as db, db:
            db.execute("INSERT INTO frontier(url,host,priority,updated,depth,parent) VALUES(?,?,?,?,?,?) ON CONFLICT(url) DO UPDATE SET priority=max(priority,excluded.priority),updated=excluded.updated", (url, p.hostname.lower(), float(priority), time.time(), int(depth), parent))

    def take_records(self, limit=100, per_host=5, max_attempts=3, exclude_hosts=()):
        if limit < 0 or per_host < 1:
            raise ValueError("invalid frontier bounds")
        selected, counts = [], {}
        with closing(self.connect()) as db, db:
            rows = db.execute("SELECT url,host,priority,depth,parent,attempts FROM frontier WHERE state='queued' AND attempts<? ORDER BY priority DESC,url", (max_attempts,)).fetchall()
            for url, host, priority, depth, parent, attempts in rows:
                if len(selected) >= limit:
                    break
                if host in exclude_hosts:
                    continue
                if counts.get(host, 0) >= per_host:
                    continue
                selected.append({"url": url, "host": host, "priority": priority, "depth": depth, "parent": parent, "attempts": attempts+1}); counts[host] = counts.get(host, 0) + 1
            db.executemany("UPDATE frontier SET state='in_progress',attempts=attempts+1,updated=? WHERE url=?", [(time.time(), row["url"]) for row in selected])
        return selected

    def take(self, limit=100, per_host=5, max_attempts=3):
        return [row["url"] for row in self.take_records(limit, per_host, max_attempts)]

    def reserve_host_request(self, host, max_requests=100, window_seconds=86400):
        """Persist a rolling fixed-window request budget per host."""
        now = time.time()
        with closing(self.connect()) as db, db:
            row = db.execute("SELECT count,window FROM host_budget WHERE host=?", (host.lower(),)).fetchone()
            if not row or now - row[1] >= window_seconds:
                db.execute("INSERT INTO host_budget VALUES(?,?,?) ON CONFLICT(host) DO UPDATE SET count=1,window=excluded.window", (host.lower(), 1, now))
                return True
            if row[0] >= max_requests:
                return False
            db.execute("UPDATE host_budget SET count=count+1 WHERE host=?", (host.lower(),))
            return True

    def finish(self, url, success):
        with closing(self.connect()) as db, db:
            db.execute("UPDATE frontier SET state=?,updated=? WHERE url=?", ("done" if success else "queued", time.time(), url))

    def defer(self, url):
        with closing(self.connect()) as db, db:
            db.execute("UPDATE frontier SET state='queued',attempts=max(0,attempts-1),updated=? WHERE url=? AND state='in_progress'", (time.time(), url))

    def checkpoint(self, name, value):
        with closing(self.connect()) as db, db:
            db.execute("INSERT INTO checkpoints VALUES(?,?,?) ON CONFLICT(name) DO UPDATE SET value=excluded.value,updated=excluded.updated", (name, json.dumps(value), time.time()))

    def load_checkpoint(self, name, default=None):
        with closing(self.connect()) as db:
            row = db.execute("SELECT value FROM checkpoints WHERE name=?", (name,)).fetchone()
        return json.loads(row[0]) if row else default

    def record_page(self, page):
        with closing(self.connect()) as db, db:
            db.execute("INSERT OR IGNORE INTO crawl_pages VALUES(?,?,?,?,?,?)", (page["url"], page["fingerprint"], page.get("status"), page.get("title", ""), page.get("observed_at", time.time()), json.dumps(page, sort_keys=True)))

    def record_edge(self, source, target, kind="link"):
        with closing(self.connect()) as db, db:
            db.execute("INSERT OR IGNORE INTO crawl_edges VALUES(?,?,?,?)", (source, target, kind, time.time()))

    def recover_frontier(self):
        """Return in-progress URLs to the queue after an interrupted worker."""
        with closing(self.connect()) as db, db:
            return db.execute("UPDATE frontier SET state='queued',updated=? WHERE state='in_progress'", (time.time(),)).rowcount

    def save_company_lead(self, lead, source):
        key = str(lead.get("organization") or lead.get("host") or lead.get("domain") or "").strip().lower()
        if not key:
            raise ValueError("company lead needs a stable organization or host")
        verified = bool(lead.get("verified_employer", lead.get("live_verified", False)))
        lead_id = source + ":" + key
        with closing(self.connect()) as db, db:
            db.execute("INSERT INTO company_leads VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET observed=excluded.observed,payload=excluded.payload,verified=max(company_leads.verified,excluded.verified)", (lead_id, key, source, int(verified), time.time(), json.dumps(lead, sort_keys=True)))
        return lead_id

    def company_leads(self, verified_only=False):
        with closing(self.connect()) as db:
            query = "SELECT id,organization,source,verified,observed,payload FROM company_leads"
            if verified_only:
                query += " WHERE verified=1"
            query += " ORDER BY organization"
            return [dict(id=row[0], organization=row[1], source=row[2], verified=bool(row[3]), observed=row[4], payload=json.loads(row[5])) for row in db.execute(query)]
