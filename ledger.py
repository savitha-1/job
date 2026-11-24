"""Local durable application review ledger; no automatic submission."""
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from urllib.parse import urlsplit, urlunsplit

def canonical(url):
    p=urlsplit(url)
    if p.scheme!="https" or not p.hostname or p.username or p.password:
        raise ValueError("expected HTTPS application URL")
    return urlunsplit(("https",p.netloc.lower(),p.path.rstrip("/") or "/",p.query,""))

class Ledger:
    def __init__(self,path):
        self.path=path
        with closing(self.connect()) as db:
            db.execute("CREATE TABLE IF NOT EXISTS applications (url TEXT PRIMARY KEY, employer TEXT NOT NULL, state TEXT NOT NULL, updated TEXT NOT NULL, evidence TEXT, batch TEXT NOT NULL DEFAULT '')")
            columns={row[1] for row in db.execute("PRAGMA table_info(applications)")}
            if "batch" not in columns:
                db.execute("ALTER TABLE applications ADD COLUMN batch TEXT NOT NULL DEFAULT ''")
            db.commit()
    def connect(self):
        return sqlite3.connect(self.path,timeout=10)
    def enqueue(self,url,employer,batch_id=None):
        if not employer.strip():
            raise ValueError("employer required")
        employer=employer.strip().casefold()
        batch_id=batch_id or datetime.now(timezone.utc).date().isoformat()
        with closing(self.connect()) as db, db:
            existing=db.execute("SELECT url FROM applications WHERE employer=? AND batch=? AND state NOT IN ('rejected')",(employer,batch_id)).fetchone()
            if existing and canonical(url)!=existing[0]:
                return False
            cur=db.execute("INSERT OR IGNORE INTO applications(url,employer,state,updated,evidence,batch) VALUES (?,?,?,?,?,?)",(canonical(url),employer,"review",datetime.now(timezone.utc).isoformat(),None,batch_id))
            return cur.rowcount==1
    def transition(self,url,expected,new,evidence=None):
        allowed={("review","prepared"),("prepared","human_required"),("human_required","prepared"),("prepared","confirmed"),("review","confirmed"),("review","rejected"),("prepared","rejected")}
        if (expected,new) not in allowed or (new=="confirmed" and not evidence):
            raise ValueError("invalid transition or missing confirmation")
        with closing(self.connect()) as db, db:
            cur=db.execute("UPDATE applications SET state=?, updated=?, evidence=? WHERE url=? AND state=?",(new,datetime.now(timezone.utc).isoformat(),evidence,canonical(url),expected))
            return cur.rowcount==1
    def rows(self):
        with closing(self.connect()) as db:
            db.row_factory=sqlite3.Row
            return [dict(r) for r in db.execute("SELECT * FROM applications ORDER BY updated DESC")]
