"""SQLite store for source-attributed, evidence-tiered opportunity discovery."""
import hashlib
import json
import re
import sqlite3
import time
import unicodedata
from contextlib import closing
from pathlib import Path
from urllib.parse import urlsplit

from engine import eligibility_evidence
from job_sources import freshness
from network_guard import public_url as public_https_url
from relevance import score_job


def employer_key(name):
    ascii_name = unicodedata.normalize("NFKD", str(name)).encode("ascii", "ignore").decode().casefold()
    return re.sub(r"[^a-z0-9]+", "-", ascii_name).strip("-")[:150] or "unknown-employer"


class OpportunityStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with closing(self.connect()) as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS discovery_runs(id INTEGER PRIMARY KEY, started REAL NOT NULL, finished REAL, state TEXT NOT NULL, source_results TEXT NOT NULL DEFAULT '{}', error TEXT);
                CREATE TABLE IF NOT EXISTS employer_records(employer_key TEXT PRIMARY KEY,name TEXT NOT NULL,first_seen REAL NOT NULL,last_seen REAL NOT NULL,sources TEXT NOT NULL DEFAULT '[]',country_hints TEXT NOT NULL DEFAULT '[]',identity_basis TEXT NOT NULL DEFAULT 'unresolved_lead');
                CREATE TABLE IF NOT EXISTS company_graph_nodes(node_key TEXT PRIMARY KEY,node_type TEXT NOT NULL,label TEXT NOT NULL,source TEXT NOT NULL,metadata TEXT NOT NULL,observed REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS company_graph_edges(source_key TEXT NOT NULL,target_key TEXT NOT NULL,relation TEXT NOT NULL,evidence_url TEXT NOT NULL,confidence REAL NOT NULL,observed REAL NOT NULL,PRIMARY KEY(source_key,target_key,relation,evidence_url));
                CREATE TABLE IF NOT EXISTS source_cache(cache_key TEXT PRIMARY KEY,fetched REAL NOT NULL,payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS opportunities(id TEXT PRIMARY KEY,employer_key TEXT NOT NULL,source TEXT NOT NULL,source_id TEXT NOT NULL,title TEXT NOT NULL,location TEXT NOT NULL,description TEXT NOT NULL,source_url TEXT NOT NULL,official_url TEXT,published_at TEXT,observed_at TEXT NOT NULL,freshness TEXT NOT NULL,eligibility TEXT NOT NULL,eligibility_evidence TEXT NOT NULL,tech_matches TEXT NOT NULL,source_active INTEGER NOT NULL,vacancy_verified INTEGER NOT NULL DEFAULT 0,official_url_verified INTEGER NOT NULL DEFAULT 0,payload TEXT NOT NULL,UNIQUE(source,source_id));
                CREATE INDEX IF NOT EXISTS opportunities_recent ON opportunities(observed_at DESC);
                CREATE INDEX IF NOT EXISTS opportunities_employer ON opportunities(employer_key);
                CREATE TABLE IF NOT EXISTS application_events(id INTEGER PRIMARY KEY,workflow_id TEXT NOT NULL,kind TEXT NOT NULL,note TEXT NOT NULL,occurred REAL NOT NULL);
            """)
            columns = {row[1] for row in db.execute("PRAGMA table_info(opportunities)")}
            if "vacancy_verified" not in columns:
                db.execute("ALTER TABLE opportunities ADD COLUMN vacancy_verified INTEGER NOT NULL DEFAULT 0")
            employer_columns = {row[1] for row in db.execute("PRAGMA table_info(employer_records)")}
            if "identity_basis" not in employer_columns:
                db.execute("ALTER TABLE employer_records ADD COLUMN identity_basis TEXT NOT NULL DEFAULT 'unresolved_lead'")
            db.commit()

    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=15000")
        return db

    def begin_run(self):
        with closing(self.connect()) as db, db:
            cur = db.execute("INSERT INTO discovery_runs(started,state) VALUES(?,?)", (time.time(), "running"))
            return cur.lastrowid

    def save_company_lead(self, *, name, source, evidence_url, homepage_claim=None, metadata=None, verified_employer=False, identity_key=None):
        """Store provenance-bearing employer candidates without implying employer verification."""
        name_key = employer_key(name)
        domain = urlsplit(homepage_claim).hostname.casefold() if homepage_claim and public_https_url(homepage_claim) else None
        if verified_employer and domain:
            key = "company:domain:" + domain
        elif identity_key:
            key = "company:identity:" + re.sub(r"[^a-z0-9._:-]+", "-", str(identity_key).casefold())[:180]
        else:
            key = "lead:" + source + ":" + hashlib.sha256((name_key + "\0" + evidence_url).encode()).hexdigest()
        now = time.time()
        lead_type = "verified_employer" if verified_employer else "unverified_company_lead"
        with closing(self.connect()) as db, db:
            db.execute("INSERT INTO company_graph_nodes VALUES(?,?,?,?,?,?) ON CONFLICT(node_key) DO UPDATE SET observed=excluded.observed,metadata=excluded.metadata,node_type=CASE WHEN excluded.node_type='verified_employer' THEN excluded.node_type ELSE company_graph_nodes.node_type END", (key, lead_type, name[:200], source, json.dumps(metadata or {}, ensure_ascii=False), now))
            source_key = "evidence:" + hashlib.sha256((source + evidence_url).encode()).hexdigest()
            db.execute("INSERT OR IGNORE INTO company_graph_nodes VALUES(?,?,?,?,?,?)", (source_key, "evidence", source, source, "{}", now))
            db.execute("INSERT OR IGNORE INTO company_graph_edges VALUES(?,?,?,?,?,?)", (key, source_key, "observed_in", evidence_url, 1.0, now))
            if homepage_claim and public_https_url(homepage_claim):
                domain_key = "domain:" + urlsplit(homepage_claim).hostname.casefold()
                db.execute("INSERT OR IGNORE INTO company_graph_nodes VALUES(?,?,?,?,?,?)", (domain_key, "unverified_domain_claim", urlsplit(homepage_claim).hostname, source, "{}", now))
                db.execute("INSERT OR IGNORE INTO company_graph_edges VALUES(?,?,?,?,?,?)", (key, domain_key, "claims_homepage", evidence_url, 0.35, now))
        return key

    def graph_summary(self):
        with closing(self.connect()) as db:
            return {"nodes": db.execute("SELECT count(*) FROM company_graph_nodes").fetchone()[0],
                    "edges": db.execute("SELECT count(*) FROM company_graph_edges").fetchone()[0],
                    "unverified_company_leads": db.execute("SELECT count(*) FROM company_graph_nodes WHERE node_type='unverified_company_lead'").fetchone()[0],
                    "verified_employers": db.execute("SELECT count(*) FROM company_graph_nodes WHERE node_type='verified_employer'").fetchone()[0]}

    def cached(self, key, max_age_seconds):
        with closing(self.connect()) as db:
            row = db.execute("SELECT fetched,payload FROM source_cache WHERE cache_key=?", (key,)).fetchone()
        return json.loads(row[1]) if row and time.time() - row[0] <= max_age_seconds else None

    def cache(self, key, payload):
        with closing(self.connect()) as db, db:
            db.execute("INSERT INTO source_cache VALUES(?,?,?) ON CONFLICT(cache_key) DO UPDATE SET fetched=excluded.fetched,payload=excluded.payload", (key, time.time(), json.dumps(payload, ensure_ascii=False)))

    def finish_run(self, run_id, source_results, error=None, state=None):
        with closing(self.connect()) as db, db:
            final_state = state or ("failed" if error else "completed")
            db.execute("UPDATE discovery_runs SET finished=?,state=?,source_results=?,error=? WHERE id=?", (time.time(), final_state, json.dumps(source_results, sort_keys=True), error, run_id))

    def deactivate_source(self, source):
        """Mark an API snapshot's prior rows inactive after a successful refresh."""
        with closing(self.connect()) as db, db:
            db.execute("UPDATE opportunities SET source_active=0 WHERE source=?", (source,))

    def save_opportunity(self, row, skills):
        domain = row.get("employer_domain")
        if domain and row.get("employer_domain_verified"):
            host = urlsplit(domain if "://" in domain else "https://" + domain).hostname
            key, identity_basis = "domain:" + host.casefold(), "verified_domain"
        elif row.get("company_identity"):
            key, identity_basis = "identity:" + str(row["company_identity"]).casefold(), "stable_source_identity"
        else:
            # Do not merge unrelated companies with the same display name.
            # Until domain/organization evidence exists, this is one lead per
            # source requisition, not a claim that two leads share an employer.
            key = "lead:" + hashlib.sha256((str(row["source"]) + "\0" + str(row["source_id"])).encode()).hexdigest()
            identity_basis = "unresolved_lead"
        observed = time.time()
        freshness_label = freshness(row)
        eligibility, evidence = eligibility_evidence(row.get("location"), row.get("description"), title=row.get("title"))
        relevance = score_job(row, skills)
        matches = [item["skill"] for item in relevance["matches"]]
        vacancy_verified = bool(row.get("vacancy_verified"))
        is_verified = bool(row.get("official_url_verified")) and bool(row.get("official_url"))
        row_id = hashlib.sha256((row["source"] + "\0" + str(row["source_id"])).encode()).hexdigest()
        public_payload = dict(row)
        public_payload["relevance"] = relevance
        with closing(self.connect()) as db, db:
            db.execute("""INSERT INTO employer_records(employer_key,name,first_seen,last_seen,sources,country_hints,identity_basis) VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(employer_key) DO UPDATE SET last_seen=excluded.last_seen,
                sources=json_insert(employer_records.sources,'$[#]',json_extract(excluded.sources,'$[0]')),
                country_hints=CASE WHEN excluded.country_hints='[]' THEN employer_records.country_hints ELSE excluded.country_hints END""",
                (key, row["employer"], observed, observed, json.dumps([row["source"]]), json.dumps([row["location"]] if row.get("location") else []), identity_basis))
            db.execute("""INSERT INTO opportunities(id,employer_key,source,source_id,title,location,description,source_url,official_url,published_at,observed_at,freshness,eligibility,eligibility_evidence,tech_matches,source_active,vacancy_verified,official_url_verified,payload) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(source,source_id) DO UPDATE SET title=excluded.title,location=excluded.location,description=excluded.description,source_url=excluded.source_url,official_url=excluded.official_url,published_at=excluded.published_at,observed_at=excluded.observed_at,freshness=excluded.freshness,eligibility=excluded.eligibility,eligibility_evidence=excluded.eligibility_evidence,tech_matches=excluded.tech_matches,source_active=excluded.source_active,vacancy_verified=excluded.vacancy_verified,official_url_verified=excluded.official_url_verified,payload=excluded.payload""",
                (row_id,key,row["source"],str(row["source_id"]),row["title"],row.get("location", ""),row.get("description", ""),row["source_url"],row.get("official_url"),row.get("published_at"),row.get("observed_at", ""),freshness_label,eligibility,json.dumps(evidence),json.dumps(matches),1,int(vacancy_verified),int(is_verified),json.dumps(public_payload,ensure_ascii=False)))
        return row_id

    def record_event(self, workflow_id, kind, note):
        allowed = {"submitted_self_report", "confirmation", "interview", "recruiter_response", "rejected", "offer", "note"}
        if kind not in allowed or not str(note).strip():
            raise ValueError("invalid application event")
        with closing(self.connect()) as db, db:
            db.execute("INSERT INTO application_events(workflow_id,kind,note,occurred) VALUES(?,?,?,?)", (workflow_id, kind, str(note)[:2000], time.time()))

    def events(self, workflow_id=None):
        with closing(self.connect()) as db:
            sql = "SELECT workflow_id,kind,note,occurred FROM application_events"
            args = ()
            if workflow_id:
                sql += " WHERE workflow_id=?"
                args = (workflow_id,)
            sql += " ORDER BY occurred DESC LIMIT 500"
            return [dict(r) for r in db.execute(sql, args)]

    def stats(self):
        with closing(self.connect()) as db:
            employer_count = db.execute("SELECT count(*) FROM employer_records WHERE identity_basis IN ('verified_domain','stable_source_identity')").fetchone()[0] + db.execute("SELECT count(*) FROM company_graph_nodes WHERE node_type='verified_employer'").fetchone()[0]
            provisional_count = db.execute("SELECT count(*) FROM employer_records WHERE identity_basis='unresolved_lead'").fetchone()[0] + db.execute("SELECT count(*) FROM company_graph_nodes WHERE node_type='unverified_company_lead'").fetchone()[0]
            vacancy_count = db.execute("SELECT count(*) FROM opportunities WHERE source_active=1").fetchone()[0]
            verified_count = db.execute("SELECT count(*) FROM opportunities WHERE vacancy_verified=1 AND source_active=1").fetchone()[0]
            official_active_count = db.execute("SELECT count(*) FROM opportunities WHERE official_url_verified=1 AND source_active=1").fetchone()[0]
            fresh_official_count = db.execute("SELECT count(*) FROM opportunities WHERE official_url_verified=1 AND source_active=1 AND freshness='fresh'").fetchone()[0]
            eligible = db.execute("SELECT count(*) FROM opportunities WHERE vacancy_verified=1 AND official_url_verified=1 AND source_active=1 AND freshness='fresh' AND eligibility='eligible_signal'").fetchone()[0]
            unknown = db.execute("SELECT count(*) FROM opportunities WHERE eligibility='unknown'").fetchone()[0]
            runs = db.execute("SELECT * FROM discovery_runs ORDER BY id DESC LIMIT 20").fetchall()
            return {"employers_discovered": employer_count, "source_active_vacancies": vacancy_count,
                    "verified_employers": db.execute("SELECT count(*) FROM company_graph_nodes WHERE node_type='verified_employer'").fetchone()[0],
                    "unverified_company_leads": provisional_count,
                    "verified_vacancies": verified_count,
                    "official_apply_urls": official_active_count,
                    "official_ats_vacancies": official_active_count,
                    "fresh_official_vacancies": fresh_official_count, "fresh_eligible_opportunities": eligible,
                    "eligibility_unknown": unknown, "runs": [dict(r) for r in runs]}

    def opportunities(self, limit=300, eligible_only=False):
        with closing(self.connect()) as db:
            where = "WHERE freshness!='stale'" + (" AND vacancy_verified=1 AND official_url_verified=1 AND source_active=1 AND freshness='fresh' AND eligibility='eligible_signal'" if eligible_only else "")
            rows = db.execute(f"SELECT id,employer_key,source,title,location,source_url,official_url,published_at,observed_at,freshness,eligibility,eligibility_evidence,tech_matches,source_active,vacancy_verified,official_url_verified,payload FROM opportunities {where} ORDER BY official_url_verified DESC, vacancy_verified DESC, (freshness='fresh') DESC, (eligibility='eligible_signal') DESC, observed_at DESC LIMIT ?", (limit,)).fetchall()
            result = []
            for r in rows:
                row = dict(r)
                for field in ("eligibility_evidence", "tech_matches", "payload"):
                    row[field] = json.loads(row[field])
                result.append(row)
            return result
