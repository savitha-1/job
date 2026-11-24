"""Persistent human-supervised application preparation workflow.

This module coordinates review, reusable approved answers, multi-step schemas,
pause/resume, and uncertain-submission recovery. It does not itself perform a
submission. Before any submit adapter exists, users can still prepare a durable
review packet and resume a workflow without repeating answered questions.
"""
import hashlib
import json
import os
import re
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from ledger import Ledger, canonical

TERMINAL = {"confirmed", "rejected", "cancelled"}
ALLOWED = {
    "review": {"preparing", "paused", "rejected", "cancelled"},
    "preparing": {"review", "human_required", "ready", "paused", "failed"},
    "human_required": {"preparing", "paused", "rejected", "cancelled"},
    "ready": {"preparing", "submission_authorized", "paused", "rejected", "cancelled"},
    "submission_authorized": {"submitting", "paused", "cancelled"},
    "submitting": {"uncertain_submission", "submitted_unconfirmed", "failed"},
    "uncertain_submission": {"confirmed", "failed", "cancelled"},
    "submitted_unconfirmed": {"confirmed", "uncertain_submission", "failed"},
    "paused": {"preparing", "ready", "human_required", "cancelled"},
    "failed": {"preparing", "cancelled"},
}


def _question_key(field):
    text = " ".join(str(field.get(k) or "") for k in ("label", "name", "placeholder"))
    text = re.sub(r"\s+", " ", text).strip().casefold()
    choices = sorted(str(x.get("label", x.get("value", ""))).strip().casefold() for x in field.get("options", []))
    raw = json.dumps([text, field.get("type", ""), choices], separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(raw.encode()).hexdigest()


def _workflow_url(url):
    """Canonicalize a persisted URL, with loopback HTTP reserved for fixtures."""
    parts = urlsplit(url)
    if parts.scheme == "http" and parts.hostname in ("localhost", "127.0.0.1", "::1") and not parts.username and not parts.password:
        return urlunsplit(("http", parts.netloc.lower(), parts.path.rstrip("/") or "/", parts.query, ""))
    return canonical(url)


class ApplicationWorkflow:
    def __init__(self, path):
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.ledger = Ledger(self.path)
        self._ensure_schema()
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass
        self.recover_interrupted_submissions()
        self.recover_interrupted_preparation()

    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.execute("PRAGMA busy_timeout=15000")
        return db

    def _ensure_schema(self):
        with closing(self.connect()) as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS workflows (
                    id TEXT PRIMARY KEY, url TEXT NOT NULL UNIQUE, employer TEXT NOT NULL,
                    batch TEXT NOT NULL, state TEXT NOT NULL, current_step INTEGER NOT NULL DEFAULT 0,
                    automation_permitted INTEGER NOT NULL DEFAULT 0, submission_scope TEXT,
                    created REAL NOT NULL, updated REAL NOT NULL, evidence TEXT, failure TEXT
                );
                CREATE TABLE IF NOT EXISTS workflow_steps (
                    workflow_id TEXT NOT NULL, step INTEGER NOT NULL, url TEXT NOT NULL,
                    schema_json TEXT NOT NULL, status TEXT NOT NULL, updated REAL NOT NULL,
                    PRIMARY KEY(workflow_id,step)
                );
                CREATE TABLE IF NOT EXISTS approval_questions (
                    question_key TEXT PRIMARY KEY, question TEXT NOT NULL, field_type TEXT NOT NULL,
                    choices TEXT NOT NULL, answer_json TEXT, approved INTEGER NOT NULL DEFAULT 0,
                    created REAL NOT NULL, updated REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS workflow_questions (
                    workflow_id TEXT NOT NULL, step INTEGER NOT NULL, field_id TEXT NOT NULL,
                    question_key TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                    PRIMARY KEY(workflow_id,step,field_id)
                );
                CREATE TABLE IF NOT EXISTS workflow_control (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated REAL NOT NULL);
                CREATE UNIQUE INDEX IF NOT EXISTS workflow_employer_batch ON workflows(lower(employer),batch);
            """)
            db.commit()

    def create(self, url, employer, batch_id, *, automation_permitted=False):
        url = _workflow_url(url)
        if not employer or not employer.strip() or not batch_id:
            raise ValueError("employer and explicit batch_id are required")
        workflow_id = hashlib.sha256((url + "\0" + batch_id).encode()).hexdigest()
        with closing(self.connect()) as db:
            existing = db.execute("SELECT employer FROM workflows WHERE id=?", (workflow_id,)).fetchone()
        if existing:
            if existing[0].casefold() != employer.strip().casefold():
                raise ValueError("the same application URL is already associated with a different employer")
            return workflow_id
        fixture_url = urlsplit(url).scheme == "http"
        inserted = True if fixture_url else self.ledger.enqueue(url, employer, batch_id)
        if not inserted and not any(row["url"] == url and row.get("batch") == batch_id for row in self.ledger.rows()):
            raise ValueError("another opportunity for this employer is already queued in this batch")
        now = time.time()
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            try:
                db.execute("INSERT INTO workflows VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (workflow_id, url, employer.strip(), batch_id, "review", 0, int(automation_permitted), None, now, now, None, None))
            except sqlite3.IntegrityError as exc:
                raise ValueError("an opportunity for this employer or URL is already queued in this batch") from exc
        return workflow_id

    def record_step(self, workflow_id, step, url, fields, status="scanned", unresolved_field_ids=None):
        if step < 0 or status not in ("scanned", "prepared", "human_required", "complete"):
            raise ValueError("invalid workflow step")
        safe_fields = []
        for field in fields:
            safe_fields.append({k: field.get(k) for k in ("id", "label", "name", "placeholder", "type", "required", "disabled", "options")})
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("INSERT INTO workflow_steps VALUES(?,?,?,?,?,?) ON CONFLICT(workflow_id,step) DO UPDATE SET url=excluded.url,schema_json=excluded.schema_json,status=excluded.status,updated=excluded.updated", (workflow_id, step, _workflow_url(url), json.dumps(safe_fields, sort_keys=True), status, time.time()))
            db.execute("UPDATE workflows SET current_step=max(current_step,?),updated=? WHERE id=?", (step, time.time(), workflow_id))
        unresolved = {str(x) for x in (unresolved_field_ids or [])}
        return self.request_approvals(workflow_id, step, [f for f in fields if str(f.get("id", "")) in unresolved])

    def plan_step(self, workflow_id, step, fields, profile, url=None):
        from application_planner import plan
        mapped = plan(fields, profile)
        unresolved_ids = {str(x.get("id")) for x in mapped["review"]}
        prepared = []
        for field in fields:
            item = dict(field)
            if str(field.get("id", "")) in unresolved_ids:
                answer = self.approved_answer(field)
                if answer is not None:
                    item["approved_value"] = answer
            prepared.append(item)
        result = plan(prepared, profile)
        pending = self.record_step(workflow_id, step, url or self.get(workflow_id)["url"], prepared, status="human_required" if result["review"] else "prepared", unresolved_field_ids=[x.get("id") for x in result["review"]])
        current = self.get(workflow_id)
        if current and current["state"] in ("review", "preparing", "human_required", "failed"):
            target = "human_required" if pending else "ready"
            with closing(self.connect()) as db, db:
                db.execute("UPDATE workflows SET state=?,updated=? WHERE id=?", (target, time.time(), workflow_id))
        if pending:
            return result, pending
        return result, []

    def request_approvals(self, workflow_id, step, fields):
        pending = []
        with closing(self.connect()) as db, db:
            for field in fields:
                key = _question_key(field)
                label = " ".join(str(field.get(k) or "") for k in ("label", "name", "placeholder")).strip()
                if not field.get("required") and not label:
                    continue
                options = field.get("options") or []
                if not label:
                    continue
                question = {"label": label, "type": field.get("type", "text"), "options": options}
                row = db.execute("SELECT approved,answer_json FROM approval_questions WHERE question_key=?", (key,)).fetchone()
                if not row:
                    now = time.time()
                    db.execute("INSERT INTO approval_questions VALUES(?,?,?,?,NULL,0,?,?)", (key, label, str(field.get("type", "text")), json.dumps(options, sort_keys=True), now, now))
                    approved = False
                else:
                    approved = bool(row[0])
                db.execute("INSERT INTO workflow_questions VALUES(?,?,?,?,?) ON CONFLICT(workflow_id,step,field_id) DO UPDATE SET question_key=excluded.question_key,status=excluded.status", (workflow_id, step, str(field.get("id", "")), key, "approved" if approved else "pending"))
                if not approved:
                    pending.append({"workflow_id": workflow_id, "step": step, "field_id": str(field.get("id", "")), "question_key": key, "question": question})
        return pending

    def approve_answer(self, question_key, answer):
        if answer is None:
            raise ValueError("an explicit answer is required")
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            cur = db.execute("UPDATE approval_questions SET answer_json=?,approved=1,updated=? WHERE question_key=?", (json.dumps(answer, ensure_ascii=False), time.time(), question_key))
            if cur.rowcount != 1:
                raise KeyError("unknown approval question")
            db.execute("UPDATE workflow_questions SET status='approved' WHERE question_key=?", (question_key,))

    def approved_answer(self, field):
        key = _question_key(field)
        with closing(self.connect()) as db:
            row = db.execute("SELECT answer_json FROM approval_questions WHERE question_key=? AND approved=1", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def approval_queue(self, workflow_id=None):
        with closing(self.connect()) as db:
            query = "SELECT wq.workflow_id,wq.step,wq.field_id,wq.question_key,aq.question,aq.field_type,aq.choices FROM workflow_questions wq JOIN approval_questions aq USING(question_key) WHERE wq.status='pending'"
            args = ()
            if workflow_id:
                query += " AND wq.workflow_id=?"
                args = (workflow_id,)
            return [dict(workflow_id=r[0], step=r[1], field_id=r[2], question_key=r[3], question=r[4], field_type=r[5], choices=json.loads(r[6])) for r in db.execute(query, args)]

    def transition(self, workflow_id, expected, new, *, failure=None, evidence=None):
        if new not in ALLOWED.get(expected, set()):
            raise ValueError("invalid workflow state transition")
        if new == "confirmed" and not evidence:
            raise ValueError("confirmation evidence is required")
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            cur = db.execute("UPDATE workflows SET state=?,failure=?,evidence=?,updated=? WHERE id=? AND state=?", (new, failure, evidence, time.time(), workflow_id, expected))
            return cur.rowcount == 1

    def authorize_submission(self, workflow_id, exact_url, scope):
        if not scope or _workflow_url(exact_url) == "":
            raise ValueError("explicit submission scope and exact URL are required")
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT url,state,automation_permitted FROM workflows WHERE id=?", (workflow_id,)).fetchone()
            if not row or row[0] != _workflow_url(exact_url) or row[1] != "ready" or not row[2]:
                raise ValueError("workflow is not ready or exact submission scope does not match")
            db.execute("UPDATE workflows SET state='submission_authorized',submission_scope=?,updated=? WHERE id=?", (scope.strip(), time.time(), workflow_id))

    def begin_submission(self, workflow_id):
        """Persist the irreversible boundary before a submit adapter is invoked."""
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            cur = db.execute("UPDATE workflows SET state='submitting',updated=? WHERE id=? AND state='submission_authorized'", (time.time(), workflow_id))
            return cur.rowcount == 1

    def recover_interrupted_submissions(self):
        with closing(self.connect()) as db, db:
            return db.execute("UPDATE workflows SET state='uncertain_submission',failure='process_interrupted_during_submission',updated=? WHERE state='submitting'", (time.time(),)).rowcount

    def recover_interrupted_preparation(self):
        with closing(self.connect()) as db, db:
            return db.execute("UPDATE workflows SET state='review',updated=? WHERE state='preparing'", (time.time(),)).rowcount

    def confirm(self, workflow_id, evidence):
        if not evidence:
            raise ValueError("confirmation evidence is required")
        row = self.get(workflow_id)
        if not row or row["state"] not in ("uncertain_submission", "submitted_unconfirmed"):
            raise ValueError("workflow is not awaiting confirmation")
        return self.transition(workflow_id, row["state"], "confirmed", evidence=evidence)

    def set_control(self, key, value):
        if key not in ("paused", "stopped"):
            raise ValueError("unknown workflow control")
        with closing(self.connect()) as db, db:
            db.execute("INSERT INTO workflow_control VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated=excluded.updated", (key, json.dumps(bool(value)), time.time()))

    def control(self):
        with closing(self.connect()) as db:
            return {r[0]: json.loads(r[1]) for r in db.execute("SELECT key,value FROM workflow_control")}

    def get(self, workflow_id):
        with closing(self.connect()) as db:
            row = db.execute("SELECT id,url,employer,batch,state,current_step,automation_permitted,submission_scope,updated,evidence,failure FROM workflows WHERE id=?", (workflow_id,)).fetchone()
            return dict(zip(("id", "url", "employer", "batch", "state", "current_step", "automation_permitted", "submission_scope", "updated", "evidence", "failure"), row)) if row else None

    def list_workflows(self):
        with closing(self.connect()) as db:
            return [dict(zip(("id", "url", "employer", "batch", "state", "current_step", "automation_permitted", "updated"), r)) for r in db.execute("SELECT id,url,employer,batch,state,current_step,automation_permitted,updated FROM workflows ORDER BY updated DESC")]
