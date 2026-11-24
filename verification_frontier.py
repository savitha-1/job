"""Durable, fair URL frontier for employer/vacancy verification."""
import hashlib
import json
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from urllib.parse import urldefrag, urlsplit


class VerificationFrontier:
    def __init__(self, path):
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path.touch(mode=0o600, exist_ok=True)
        try:
            self.path.chmod(0o600)
        except OSError:
            pass
        with closing(self.connect()) as db, db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS verification_targets(
                    id TEXT PRIMARY KEY, source TEXT NOT NULL, source_id TEXT NOT NULL,
                    employer TEXT NOT NULL, title TEXT NOT NULL, payload TEXT NOT NULL,
                    priority REAL NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'pending',
                    created REAL NOT NULL, updated REAL NOT NULL, last_failure TEXT,
                    UNIQUE(source,source_id));
                CREATE TABLE IF NOT EXISTS verification_urls(
                    id TEXT PRIMARY KEY, target_id TEXT NOT NULL, url TEXT NOT NULL,
                    method TEXT NOT NULL DEFAULT 'static_html', priority REAL NOT NULL DEFAULT 0,
                    stage INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'queued',
                    attempts INTEGER NOT NULL DEFAULT 0, next_retry REAL NOT NULL DEFAULT 0,
                    created REAL NOT NULL, updated REAL NOT NULL, last_failure TEXT,
                    UNIQUE(target_id,url), FOREIGN KEY(target_id) REFERENCES verification_targets(id));
                CREATE INDEX IF NOT EXISTS verify_queue ON verification_urls(status,next_retry,stage,priority DESC,created);
                CREATE TABLE IF NOT EXISTS verification_observations(
                    id INTEGER PRIMARY KEY, target_id TEXT NOT NULL, url_id TEXT,
                    url TEXT NOT NULL, method TEXT NOT NULL, outcome TEXT NOT NULL,
                    reason TEXT NOT NULL, retryable INTEGER NOT NULL, request_count INTEGER NOT NULL,
                    bytes_read INTEGER NOT NULL, elapsed REAL NOT NULL, evidence TEXT NOT NULL,
                    alternatives TEXT NOT NULL, observed REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS verification_metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            """)
        self.ensure_verifier_version("7")

    def ensure_verifier_version(self, version):
        """Requeue old evidence once when verification rules change materially."""
        with closing(self.connect()) as db, db:
            row = db.execute("SELECT value FROM verification_metadata WHERE key='verifier_version'").fetchone()
            if row and row[0] == version:
                return
            db.execute("UPDATE verification_urls SET status='queued',next_retry=0,last_failure='verification_rules_updated',updated=? WHERE status IN ('done','retry')", (time.time(),))
            db.execute("UPDATE verification_targets SET status='pending',last_failure='verification_rules_updated',updated=? WHERE status IN ('verified','exhausted')", (time.time(),))
            db.execute("INSERT INTO verification_metadata(key,value) VALUES('verifier_version',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (version,))

    def connect(self):
        db = sqlite3.connect(self.path, timeout=20)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=20000")
        db.execute("PRAGMA foreign_keys=ON")
        return db

    @staticmethod
    def _url_id(target_id, url):
        return hashlib.sha256((target_id + "\0" + url).encode()).hexdigest()

    def enqueue_candidate(self, lead, *, priority=0):
        source = str(lead.get("source") or "unknown")[:80]
        source_id = str(lead.get("source_id") or lead.get("id") or "")[:200]
        if not source_id:
            raise ValueError("candidate needs source identity")
        target_id = hashlib.sha256((source + "\0" + source_id).encode()).hexdigest()
        now = time.time()
        payload = dict(lead)
        payload["description"] = str(payload.get("description") or "")[:12000]
        with closing(self.connect()) as db, db:
            db.execute("INSERT INTO verification_targets(id,source,source_id,employer,title,payload,priority,created,updated) VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(source,source_id) DO UPDATE SET payload=excluded.payload,priority=max(verification_targets.priority,excluded.priority),updated=excluded.updated", (target_id, source, source_id, str(lead.get("employer") or lead.get("company") or "Unknown")[:200], str(lead.get("title") or "")[:300], json.dumps(payload, ensure_ascii=False), float(priority), now, now))
            existing = db.execute("SELECT count(*) FROM verification_urls WHERE target_id=?", (target_id,)).fetchone()[0]
            links = []
            for index, url in enumerate(lead.get("candidate_urls") or lead.get("candidate_application_urls") or []):
                url = urldefrag(str(url))[0]
                parsed = urlsplit(url)
                if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
                    continue
                if url in links:
                    continue
                links.append(url)
                url_id = self._url_id(target_id, url)
                rank = len(links)
                # Stage zero is one best link per employer. Only after every
                # employer has a first attempt do secondary links get claimed.
                stage = 0 if rank == 1 else 1
                link_priority = float(priority) + max(0, 50 - rank)
                db.execute("INSERT OR IGNORE INTO verification_urls(id,target_id,url,priority,stage,created,updated) VALUES(?,?,?,?,?,?,?)", (url_id,target_id,url,link_priority,stage,now,now))
        return target_id

    def claim_urls(self, limit=100, *, now=None):
        if not 1 <= limit <= 500:
            raise ValueError("verification URL batch must be between 1 and 500")
        now = now or time.time()
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute("""SELECT u.id,u.target_id,u.url,u.method,u.attempts,u.stage,
                t.source,t.source_id,t.employer,t.title,t.payload
                FROM verification_urls u JOIN verification_targets t ON t.id=u.target_id
                WHERE u.status IN ('queued','retry') AND u.next_retry<=? AND t.status!='verified'
                ORDER BY u.stage ASC,t.priority DESC,u.priority DESC,u.created ASC LIMIT ?""", (now,limit)).fetchall()
            for row in rows:
                db.execute("UPDATE verification_urls SET status='in_progress',attempts=attempts+1,updated=? WHERE id=?", (now,row["id"]))
                db.execute("UPDATE verification_targets SET status='in_progress',updated=? WHERE id=? AND status!='verified'", (now,row["target_id"]))
            db.commit()
        return [{**dict(row), "payload": json.loads(row["payload"])} for row in rows]

    def enqueue_discovered(self, target_id, links, *, stage=2):
        now = time.time()
        added = 0
        with closing(self.connect()) as db, db:
            for item in links:
                url = urldefrag(str(item.get("url", "")))[0]
                p = urlsplit(url)
                if p.scheme != "https" or not p.hostname or p.username or p.password:
                    continue
                url_id = self._url_id(target_id,url)
                cur = db.execute("INSERT OR IGNORE INTO verification_urls(id,target_id,url,method,priority,stage,created,updated) VALUES(?,?,?,?,?,?,?,?)", (url_id,target_id,url,str(item.get("method") or "static_html"),float(item.get("priority",0)),stage,now,now))
                added += cur.rowcount
        return added

    def finish_url(self, item, result):
        now = time.time()
        retryable = bool(result.get("retryable"))
        next_retry = float(result.get("next_retry", now + 900)) if retryable else 0
        status = "retry" if retryable else "done"
        failure = str(result.get("reason") or "")[:200]
        with closing(self.connect()) as db, db:
            db.execute("UPDATE verification_urls SET status=?,next_retry=?,updated=?,last_failure=? WHERE id=?", (status,next_retry,now,failure,item["id"]))
            db.execute("INSERT INTO verification_observations(target_id,url_id,url,method,outcome,reason,retryable,request_count,bytes_read,elapsed,evidence,alternatives,observed) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", (item["target_id"],item["id"],item["url"],str(result.get("method","static_html")),str(result.get("outcome","unverified")),failure,int(retryable),int(result.get("request_count",0)),int(result.get("bytes_read",0)),float(result.get("elapsed_seconds",0)),json.dumps(result.get("evidence",{}),ensure_ascii=False),json.dumps(result.get("alternatives",[]),ensure_ascii=False),now))
            if result.get("verified"):
                db.execute("UPDATE verification_targets SET status='verified',updated=?,last_failure=NULL WHERE id=?", (now,item["target_id"]))
            else:
                db.execute("UPDATE verification_targets SET last_failure=?,updated=? WHERE id=? AND status!='verified'", (failure,now,item["target_id"]))
                pending = db.execute("SELECT count(*) FROM verification_urls WHERE target_id=? AND status IN ('queued','retry','in_progress')", (item["target_id"],)).fetchone()[0]
                if not pending:
                    db.execute("UPDATE verification_targets SET status='exhausted',updated=? WHERE id=? AND status!='verified'", (now,item["target_id"]))

    def release_claim(self, item):
        """Return work claimed but not started to the durable queue."""
        with closing(self.connect()) as db, db:
            db.execute("UPDATE verification_urls SET status='queued',attempts=max(0,attempts-1),updated=? WHERE id=? AND status='in_progress'", (time.time(),item["id"]))
            db.execute("UPDATE verification_targets SET status='pending',updated=? WHERE id=? AND status!='verified'", (time.time(),item["target_id"]))

    def recover_stale(self, seconds=1800):
        cutoff = time.time() - seconds
        with closing(self.connect()) as db, db:
            return db.execute("UPDATE verification_urls SET status='retry',next_retry=0,updated=?,last_failure='worker_crash_recovered' WHERE status='in_progress' AND updated<?", (time.time(),cutoff)).rowcount

    def stats(self):
        with closing(self.connect()) as db, db:
            return {"targets": db.execute("SELECT count(*) FROM verification_targets").fetchone()[0],
                    "verified_targets": db.execute("SELECT count(*) FROM verification_targets WHERE status='verified'").fetchone()[0],
                    "pending_targets": db.execute("SELECT count(*) FROM verification_targets WHERE status IN ('pending','in_progress')").fetchone()[0],
                    "exhausted_targets": db.execute("SELECT count(*) FROM verification_targets WHERE status='exhausted'").fetchone()[0],
                    "urls_total": db.execute("SELECT count(*) FROM verification_urls").fetchone()[0],
                    "urls_pending": db.execute("SELECT count(*) FROM verification_urls WHERE status IN ('queued','retry','in_progress')").fetchone()[0],
                    "urls_attempted": db.execute("SELECT count(*) FROM verification_urls WHERE attempts>0").fetchone()[0],
                    "observations": db.execute("SELECT count(*) FROM verification_observations").fetchone()[0]}

    def observations(self, limit=500):
        with closing(self.connect()) as db, db:
            rows = db.execute("SELECT source,source_id,employer,title,url,method,outcome,reason,retryable,request_count,bytes_read,elapsed,evidence,alternatives,observed FROM verification_observations JOIN verification_targets USING(target_id) ORDER BY observed DESC LIMIT ?", (limit,)).fetchall()
            result=[]
            for row in rows:
                item=dict(row)
                for field in ("evidence","alternatives"):
                    item[field]=json.loads(item[field])
                result.append(item)
            return result
