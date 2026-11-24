"""Single-user local employment discovery and application-preparation UI.

Run with: python dashboard.py. The HTTP server binds only to 127.0.0.1.
Candidate files and profile facts stay in the configured local data directory.
"""
import argparse
import datetime as dt
import json
import os
import re
import secrets
import threading
import time
import uuid
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

from application_workflow import ApplicationWorkflow
from candidate_profile import extract_text, suggest_facts
from discover import discover as discover_ats
from engine import eligibility_evidence
from job_sources import SOURCES, fetch as fetch_source, freshness
from network_guard import public_url
from opportunity_store import OpportunityStore
from relevance import score_job

DEFAULT_SKILLS = ["Go", "Python", "TypeScript", "Node.js", "React", "Kubernetes", "PostgreSQL", "AWS"]
ATS_HOSTS = {
    "greenhouse": ("boards.greenhouse.io", "job-boards.greenhouse.io"),
    "lever": ("jobs.lever.co",),
    "lever-eu": ("jobs.eu.lever.co",),
    "ashby": ("jobs.ashbyhq.com",),
}


def _atomic_json(path, data):
    path = Path(path)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def _safe_profile(profile):
    profile = profile if isinstance(profile, dict) else {}
    facts = profile.get("facts") if isinstance(profile.get("facts"), dict) else {}
    skills = profile.get("skills") if isinstance(profile.get("skills"), list) else DEFAULT_SKILLS
    boards = profile.get("ats_boards") if isinstance(profile.get("ats_boards"), list) else []
    safe_boards = []
    for entry in boards:
        if isinstance(entry, dict):
            source, board = entry.get("source"), str(entry.get("board", ""))
        else:
            try:
                source, board = str(entry).split(":", 1)
            except ValueError:
                continue
        if source in ATS_HOSTS and re.fullmatch(r"[A-Za-z0-9_-]{1,100}", board):
            safe_boards.append({"source": source, "board": board})
    safe_facts = {}
    for key, fact in facts.items():
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,50}", str(key)) or not isinstance(fact, dict):
            continue
        value = str(fact.get("value", ""))[:2000]
        safe_facts[key] = {"value": value, "approved": fact.get("approved") is True, "source": str(fact.get("source", "user"))[:500]}
    return {"facts": safe_facts, "skills": [str(s)[:50] for s in skills[:40] if str(s).strip()],
            "ats_boards": safe_boards, "resume_name": str(profile.get("resume_name", ""))[:200],
            "gmail_connected": bool(profile.get("gmail_connected")), "resume_path": str(profile.get("resume_path", ""))[:1000]}


class ProductService:
    def __init__(self, data_dir):
        self.data_dir = Path(data_dir).expanduser().resolve()
        self.data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            os.chmod(self.data_dir, 0o700)
        except OSError:
            pass
        self.resume_dir = self.data_dir / "resumes"
        self.resume_dir.mkdir(mode=0o700, exist_ok=True)
        self.profile_path = self.data_dir / "profile.json"
        self.db_path = self.data_dir / "employment.sqlite3"
        self.discovery = OpportunityStore(self.db_path)
        self.workflows = ApplicationWorkflow(self.db_path)
        self.profile = _safe_profile(json.loads(self.profile_path.read_text()) if self.profile_path.exists() else {})
        resume_path = Path(self.profile.get("resume_path") or ".").resolve()
        if not resume_path.is_relative_to(self.resume_dir.resolve()) or not resume_path.is_file():
            self.profile.pop("resume_path", None)
        self.status_lock = threading.RLock()
        self.csrf_token = secrets.token_urlsafe(32)
        self.status = {"state": "idle", "started": None, "finished": None, "source_results": {}, "error": None}
        self.worker = None

    def save_profile(self, profile):
        self.profile = _safe_profile(profile)
        _atomic_json(self.profile_path, self.profile)
        return self.profile

    def import_resume(self, name, body):
        if len(body) > 15 * 1024 * 1024:
            raise ValueError("resume upload exceeds 15 MB")
        suffix = Path(name).suffix.lower()
        if suffix not in (".pdf", ".docx", ".txt", ".md"):
            raise ValueError("upload a PDF, DOCX, TXT, or MD resume")
        target = self.resume_dir / (uuid.uuid4().hex + suffix)
        target.write_bytes(body)
        os.chmod(target, 0o600)
        text = extract_text(target)
        facts = suggest_facts(text, target.name)
        self.profile["facts"] = {**self.profile.get("facts", {}), **facts}
        self.profile["resume_path"] = str(target)
        self.profile["resume_name"] = Path(name).name[:200]
        self.save_profile(self.profile)
        return {"resume_name": self.profile["resume_name"], "facts": self.profile["facts"]}

    def approve_facts(self, facts, skills=None):
        if not isinstance(facts, dict):
            raise ValueError("facts must be a JSON object")
        merged = dict(self.profile.get("facts", {}))
        for key, value in facts.items():
            if not re.fullmatch(r"[a-z][a-z0-9_]{0,50}", str(key)):
                continue
            if isinstance(value, dict):
                raw, approved = value.get("value"), value.get("approved") is True
                source = value.get("source", "user")
            else:
                raw, approved, source = value, True, "user-approved"
            if raw is None:
                continue
            merged[key] = {"value": str(raw)[:2000], "approved": approved, "source": str(source)[:500]}
        self.profile["facts"] = merged
        if skills is not None:
            self.profile["skills"] = [str(s).strip()[:50] for s in skills if str(s).strip()][:40]
        return self.save_profile(self.profile)

    def set_ats_board(self, source, board):
        if source not in ATS_HOSTS or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", board):
            raise ValueError("unsupported ATS board")
        boards = self.profile.get("ats_boards", [])
        if not any(x["source"] == source and x["board"].casefold() == board.casefold() for x in boards):
            boards.append({"source": source, "board": board})
        self.profile["ats_boards"] = boards
        self.save_profile(self.profile)

    def start_discovery(self, *, recurring=False):
        with self.status_lock:
            if self.worker and self.worker.is_alive():
                return False
            self.workflows.set_control("paused", False)
            self.workflows.set_control("stopped", False)
            self.worker = threading.Thread(target=self._discovery_loop, args=(recurring,), daemon=True, name="employment-discovery")
            self.worker.start()
            return True

    def pause(self):
        self.workflows.set_control("paused", True)

    def stop(self):
        self.workflows.set_control("stopped", True)
        self.workflows.set_control("paused", False)

    def _discovery_loop(self, recurring):
        while True:
            control = self.workflows.control()
            if control.get("stopped"):
                with self.status_lock:
                    self.status.update(state="stopped", finished=time.time())
                return
            if control.get("paused"):
                with self.status_lock:
                    self.status["state"] = "paused"
                time.sleep(0.5)
                continue
            self._run_discovery()
            if not recurring:
                return
            # Daily incremental refresh; stop/pause wake checks at 30 second intervals.
            for _ in range(24 * 120):
                if self.workflows.control().get("stopped") or self.workflows.control().get("paused"):
                    break
                time.sleep(30)

    def _run_discovery(self):
        run_id = self.discovery.begin_run()
        with self.status_lock:
            self.status.update(state="discovering", started=time.time(), finished=None, source_results={}, error=None)
        skills = self.profile.get("skills") or DEFAULT_SKILLS
        source_results, errors = {}, []
        selected = ["arbeitnow_de", "arbeitnow_uk", "remoteok", "jobicy"]
        futures = {}
        try:
            with ThreadPoolExecutor(max_workers=4) as pool:
                futures = {pool.submit(fetch_source, source): source for source in selected}
                for future in as_completed(futures):
                    source = futures[future]
                    control = self.workflows.control()
                    if control.get("stopped"):
                        break
                    if control.get("paused"):
                        continue
                    try:
                        rows = future.result()
                        self.discovery.deactivate_source(source)
                        kept = 0
                        for row in rows:
                            relevance = score_job(row, skills)
                            if freshness(row) == "stale" or not relevance["relevant"]:
                                continue
                            # Aggregator apply_url fields are not trusted as employer links
                            # unless a different, supported ATS domain is present.
                            if row.get("official_url") and urlsplit(row["official_url"]).hostname in {"remoteok.com", "www.remoteok.com"}:
                                row["official_url"] = None
                            row["official_url_verified"] = False
                            self.discovery.save_opportunity(row, skills)
                            kept += 1
                        source_results[source] = {"rows_received": len(rows), "engineering_matches_saved": kept, "employers_in_feed": len({row["employer"].casefold() for row in rows})}
                    except Exception as exc:
                        errors.append(f"{source}: {type(exc).__name__}")
                        source_results[source] = {"error": type(exc).__name__}

            # Add the independent monthly employer-authored source to the same
            # persistent opportunity ledger. Its claims stay unverified until
            # the public employer/ATS crawl below corroborates them.
            if not self.workflows.control().get("paused") and not self.workflows.control().get("stopped"):
                try:
                    from hn_hiring import fetch_latest
                    hn = fetch_latest(max_comments=1200)
                    saved = 0
                    for row in hn["opportunities"]:
                        if score_job(row, skills)["relevant"]:
                            self.discovery.save_opportunity(row, skills)
                            saved += 1
                    source_results["hn_whoishiring"] = {"thread": hn.get("thread"), "comments_read": hn["comments_read"], "top_level_comments": hn["top_level_comments"], "engineering_role_leads_saved": saved, "verified_vacancies": 0}
                except Exception as exc:
                    errors.append("hn_whoishiring: " + type(exc).__name__)
                    source_results["hn_whoishiring"] = {"error": type(exc).__name__}

            # GH Archive provides public engineering activity, not proof of an
            # employer. Resolve a small, high-signal sample through GitHub's org
            # API and preserve every result as an unverified company lead.
            if not self.workflows.control().get("paused") and not self.workflows.control().get("stopped"):
                try:
                    from gharchive_live import fetch_hour, resolve_public_organization
                    activity = fetch_hour(compressed_limit=8 * 1024 * 1024, expanded_limit=48 * 1024 * 1024, max_events=60_000)
                    ranked_leads = sorted(activity["leads"], key=lambda x: (x["hiring_signals"], sum(x["technologies"].values()), x["event_count"]), reverse=True)
                    resolved = 0
                    homepage_seeds = []
                    rate_cooldown = self.discovery.cached("github_org_rate_limit", 24 * 3600)
                    for lead in ranked_leads[:15]:
                        if self.workflows.control().get("paused") or self.workflows.control().get("stopped"):
                            break
                        if rate_cooldown and rate_cooldown.get("retry_at", 0) > time.time():
                            break
                        cache_key = "github_org:" + lead["organization"].casefold()
                        org = self.discovery.cached(cache_key, 7 * 86400)
                        if org is None:
                            try:
                                org = resolve_public_organization(lead["organization"])
                            except Exception as exc:
                                from gharchive_live import GitHubRateLimited
                                if isinstance(exc, GitHubRateLimited):
                                    retry_at = exc.reset_at or int(time.time()) + 3600
                                    self.discovery.cache("github_org_rate_limit", {"retry_at": retry_at})
                                    rate_cooldown = {"retry_at": retry_at}
                                    break
                                raise
                            if org:
                                self.discovery.cache(cache_key, org)
                        if not org:
                            continue
                        self.discovery.save_company_lead(name=org["name"], source="github_org_api", evidence_url=org["evidence_url"], homepage_claim=org["homepage_claim"], identity_key="github:" + org["login"], metadata={"github_login": org["login"], "location": org["location"], "public_repos": org["public_repos"], "event_count": lead["event_count"], "hiring_signals": lead["hiring_signals"], "technologies": lead["technologies"]})
                        resolved += 1
                        if org.get("homepage_claim") and len(homepage_seeds) < 3:
                            homepage_seeds.append(org["homepage_claim"])
                    crawl_result = None
                    if homepage_seeds and not self.workflows.control().get("paused") and not self.workflows.control().get("stopped"):
                        from discovery_state import DiscoveryStore
                        from web_crawler import explore_sync
                        crawl_result = explore_sync(homepage_seeds, store=DiscoveryStore(self.db_path), max_pages=12, max_depth=2, per_host=3, delay=1.0)
                    source_results["gharchive_live"] = {"hour_utc": activity["hour_utc"], "events_read": activity["events_read"], "engineering_leads": len(activity["leads"]), "org_profiles_resolved": resolved, "employer_verified": 0, "vacancies_verified": 0}
                    source_results["gharchive_live"]["homepage_claims_crawled"] = len(homepage_seeds)
                    if crawl_result is not None:
                        source_results["gharchive_live"]["crawl"] = {"pages_loaded": len(crawl_result["pages"]), "job_postings_extracted": sum(len(page["job_postings"]) for page in crawl_result["pages"]), "human_checkpoints": len(crawl_result["checkpoints"])}
                except Exception as exc:
                    errors.append("gharchive_live: " + type(exc).__name__)
                    source_results["gharchive_live"] = {"error": type(exc).__name__}

            configured = self.profile.get("ats_boards", [])
            if configured and not self.workflows.control().get("paused") and not self.workflows.control().get("stopped"):
                pairs = [(x["source"], x["board"]) for x in configured]
                jobs, ats_errors = discover_ats(pairs, workers=min(4, max(1, len(pairs))))
                if not ats_errors:
                    for source in {"ats:" + item["source"] for item in configured}:
                        self.discovery.deactivate_source(source)
                for job in jobs:
                    relevance = score_job(job, skills)
                    if not relevance["relevant"]:
                        continue
                    source = job["source"]
                    allowed_hosts = ATS_HOSTS.get(source, ())
                    host = (urlsplit(job["application_url"]).hostname or "").casefold()
                    official = host in allowed_hosts and public_url(job["application_url"])
                    row = {"source": "ats:" + source, "source_name": source.title(), "source_credit": source.title(),
                           "source_home": job["evidence_url"], "source_id": job["id"],
                           "employer": job["board"], "title": job["title"], "location": job["location"],
                           "description": job["description"], "remote": False,
                           "published_at": job.get("published_at"), "observed_at": job["observed_at"],
                           "source_url": job["evidence_url"], "official_url": job["application_url"],
                           "tags": [], "job_type": "", "seniority": "", "vacancy_verified": official,
                           "official_url_verified": official}
                    self.discovery.save_opportunity(row, skills)
                source_results["configured_ats"] = {"rows_received": len(jobs), "errors": len(ats_errors), "boards": len(configured)}
                errors.extend(f"ATS board {item.get('board')}: fetch failed" for item in ats_errors)
            if self.workflows.control().get("stopped"):
                state = "stopped"
            elif self.workflows.control().get("paused"):
                state = "paused"
            else:
                state = "partial" if errors else "completed"
            stats = self.discovery.stats()
            source_results["totals"] = {"distinct_employers": stats["employers_discovered"], "source_active_vacancies": stats["source_active_vacancies"], "verified_vacancies": stats["verified_vacancies"], "official_apply_urls": stats["official_apply_urls"], "fresh_eligible_opportunities": stats["fresh_eligible_opportunities"]}
            self.discovery.finish_run(run_id, source_results, "; ".join(errors) or None, state=state)
            with self.status_lock:
                self.status.update(state=state, finished=time.time(), source_results=source_results, error="; ".join(errors) or None)
        except Exception as exc:
            self.discovery.finish_run(run_id, source_results, type(exc).__name__)
            with self.status_lock:
                self.status.update(state="failed", finished=time.time(), error=type(exc).__name__)

    def state(self):
        stats = self.discovery.stats()
        workflows = self.workflows.list_workflows()
        counts = {state: sum(w["state"] == state for w in workflows) for state in ("review", "preparing", "human_required", "ready", "submitting", "uncertain_submission", "submitted_unconfirmed", "confirmed", "failed")}
        return {"stats": stats, "status": dict(self.status), "profile": {"facts": self.profile.get("facts", {}), "skills": self.profile.get("skills", []), "ats_boards": self.profile.get("ats_boards", []), "resume_name": self.profile.get("resume_name", ""), "gmail_connected": self.profile.get("gmail_connected", False)},
                "workflow_counts": counts, "workflows": workflows[:100], "approval_queue": self.workflows.approval_queue()[:100], "events": self.discovery.events()[:100],
                "opportunities": self.discovery.opportunities(200)}

    def prepare(self, opportunity_id):
        import asyncio
        from form_assistant import prepare_form
        record = next((x for x in self.discovery.opportunities(500) if x["id"] == opportunity_id), None)
        if not record or not record.get("official_url_verified") or not record.get("official_url"):
            raise ValueError("preparation requires a verified official ATS application URL")
        employer = record["employer_key"]
        batch = dt.date.today().isoformat()
        workflow_id = self.workflows.create(record["official_url"], employer, batch, automation_permitted=False)
        profile = {"facts": self.profile.get("facts", {})}
        packet = asyncio.run(prepare_form(record["official_url"], profile, self.profile.get("resume_path"), dry_run=True,
                                          workflow=self.workflows, workflow_id=workflow_id))
        return {"workflow_id": workflow_id, "packet": packet}

    def record_manual_submission(self, workflow_id, note):
        current = self.workflows.get(workflow_id)
        if not current or current["state"] not in ("ready", "human_required"):
            raise ValueError("workflow is not ready for a user-reported submission")
        # Self-report is explicitly labeled as such and does not claim portal confirmation.
        with self.workflows.connect() as db:
            db.execute("UPDATE workflows SET state='submitted_unconfirmed',evidence=?,updated=? WHERE id=? AND state=?", ("user_report:" + note[:1000], time.time(), workflow_id, current["state"]))
        self.discovery.record_event(workflow_id, "submitted_self_report", note)


PAGE = r"""<!doctype html><html><head><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'><title>Job Intelligence</title><style>
:root{font:15px system-ui;color:#17212b;background:#f3f6fa}body{margin:0}header{background:#14283f;color:white;padding:18px 24px;display:flex;justify-content:space-between;align-items:center}main{max-width:1250px;margin:20px auto;padding:0 16px}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px}.card,section{background:white;border:1px solid #dce3eb;border-radius:12px;padding:16px;margin-bottom:14px}.metric{font-size:28px;font-weight:700}.muted{color:#627184;font-size:13px}.row{display:flex;gap:8px;flex-wrap:wrap;align-items:center}button,.button{border:0;border-radius:7px;background:#176b87;color:white;padding:9px 13px;font:inherit;cursor:pointer;text-decoration:none}button.secondary{background:#e4edf4;color:#173246}button.danger{background:#a63d40}input,textarea,select{font:inherit;padding:8px;border:1px solid #bac8d6;border-radius:6px;max-width:100%}input[type=checkbox]{width:auto}label{display:inline-flex;gap:6px;align-items:center;margin:4px}table{width:100%;border-collapse:collapse}td,th{text-align:left;padding:9px 7px;border-bottom:1px solid #e4eaf0;vertical-align:top}th{font-size:12px;color:#536275}a{color:#075a80}.pill{display:inline-block;padding:3px 7px;border-radius:20px;background:#edf3f7;font-size:12px;margin:2px}.good{background:#e1f4e8}.warn{background:#fff1d4}.item{padding:12px 0;border-bottom:1px solid #e7edf2}details summary{cursor:pointer;font-weight:650}.hidden{display:none}pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px}.status{font-weight:650}.error{color:#a5262c}#notice{position:sticky;top:0;background:#fff1d4;padding:10px;z-index:2}
</style></head><body><header><div><strong>Job Intelligence</strong><div class=muted style=color:#c7d7e4>Local discovery · evidence-first · human controlled</div></div><div class=row><span id=status class=status>Loading…</span><button class=secondary onclick="act('/api/discovery/start',{})">Start discovery</button><button class=secondary onclick="act('/api/discovery/pause',{})">Pause</button><button class=danger onclick="act('/api/discovery/stop',{})">Stop</button></div></header><main><div id=notice class=hidden></div><div class=grid id=metrics></div>
<section><details open><summary>Candidate profile</summary><p class=muted>Resume parsing happens on this computer. Extracted facts begin unapproved. Approve only facts you have checked.</p><div class=row><input type=file id=resume accept='.pdf,.docx,.txt,.md'><button onclick=uploadResume()>Upload and parse resume</button><span id=resumeName></span></div><div id=facts></div><div class=row><label>Skills (comma separated) <input id=skills size=60></label><button class=secondary onclick=saveProfile()>Save approved facts and skills</button></div></details></section>
<section><details><summary>Expand official ATS discovery</summary><p class=muted>Public aggregators below are discovery leads. Add employer board slugs you already know to verify current jobs directly from their official ATS feeds.</p><div class=row><select id=atsSource><option value=greenhouse>Greenhouse</option><option value=lever>Lever</option><option value=lever-eu>Lever EU</option><option value=ashby>Ashby</option></select><input id=atsBoard placeholder='board slug'><button onclick=addBoard()>Add official board</button><span id=boards></span></div></details></section>
<section><h2>Discoveries</h2><p class=muted>Every listing carries source attribution. “Source active” means present in a provider feed. Only a current official ATS record with its provider-hosted application URL counts as verified official; aggregator links are never mislabeled.</p><div style=overflow:auto><table><thead><tr><th>Employer / role</th><th>Location & eligibility</th><th>Freshness / evidence</th><th>Source</th><th>Next step</th></tr></thead><tbody id=jobs></tbody></table></div></section>
<section><h2>Human review queue</h2><div id=approvals></div></section><section><h2>Applications, follow-up and interviews</h2><div id=apps></div><div class=row><select id=eventKind><option value=interview>Interview</option><option value=recruiter_response>Recruiter response</option><option value=note>Note</option><option value=rejected>Rejected</option><option value=offer>Offer</option></select><input id=eventWorkflow placeholder='workflow ID'><input id=eventNote placeholder='Short private note' size=40><button onclick=addEvent()>Add timeline event</button></div><div id=events></div></section>
<p class=muted>Local single-user service; no live application submissions are automated. A user report is labeled unconfirmed until email or other evidence is correlated.</p></main><script>
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const csrfToken='__CSRF__';
async function req(url,body){let opts={method:body===undefined?'GET':'POST',headers:{}};if(body!==undefined){opts.headers['Content-Type']='application/json';opts.headers['X-CSRF-Token']=csrfToken;opts.body=JSON.stringify(body)}let r=await fetch(url,opts),d=await r.json();if(!r.ok)throw Error(d.error||r.statusText);return d}
async function act(url,body){try{let x=await req(url,body);notice(x.message||'Updated');await refresh()}catch(e){notice(e.message,true)}}
function notice(s,bad=false){let n=document.querySelector('#notice');n.textContent=s;n.className=bad?'error':'';setTimeout(()=>n.classList.add('hidden'),7000)}
async function refresh(){try{let s=await req('/api/state');let st=s.status;document.querySelector('#status').textContent=st.state+(st.error?' · '+st.error:'');document.querySelector('#metrics').innerHTML=[['Employer names / leads',s.stats.employers_discovered],['Provider-active listings',s.stats.source_active_vacancies],['Official-source vacancies',s.stats.verified_vacancies],['Official application URLs',s.stats.official_apply_urls],['Fresh verified India eligible',s.stats.fresh_eligible_opportunities],['Prepared / confirmed',s.workflow_counts.ready+s.workflow_counts.human_required+' / '+s.workflow_counts.confirmed]].map(([a,b])=>`<div class=card><div class=muted>${esc(a)}</div><div class=metric>${b||0}</div></div>`).join('');document.querySelector('#resumeName').textContent=s.profile.resume_name||'';document.querySelector('#skills').value=(s.profile.skills||[]).join(', ');document.querySelector('#boards').textContent=(s.profile.ats_boards||[]).map(x=>x.source+':'+x.board).join(' · ');renderFacts(s.profile.facts);renderJobs(s.opportunities);renderApprovals(s.approval_queue);renderApps(s.workflows);renderEvents(s.events)}catch(e){notice(e.message,true)}}
function renderFacts(f){let box=document.querySelector('#facts');let rows=Object.entries(f||{});box.innerHTML=rows.length?rows.map(([k,v])=>`<label><input data-fact="${esc(k)}" type=checkbox ${v.approved?'checked':''}><b>${esc(k.replaceAll('_',' '))}</b><input data-value="${esc(k)}" value="${esc(v.value)}" size=22><span class=muted>${esc(v.source)}</span></label>`).join('<br>'):'<p class=muted>No extracted facts yet. Add facts after checking your resume.</p>'}
async function uploadResume(){let f=document.querySelector('#resume').files[0];if(!f)return notice('Choose a resume file.',true);let data=new FormData();data.append('resume',f);try{let r=await fetch('/api/resume',{method:'POST',headers:{'X-CSRF-Token':csrfToken},body:data}),j=await r.json();if(!r.ok)throw Error(j.error);notice('Resume parsed locally. Review and approve the suggested facts.');refresh()}catch(e){notice(e.message,true)}}
async function saveProfile(){let facts={};document.querySelectorAll('[data-value]').forEach(v=>{let k=v.dataset.value,approved=document.querySelector(`[data-fact="${CSS.escape(k)}"]`).checked;facts[k]={value:v.value,approved,source:'candidate-reviewed'}});let skills=document.querySelector('#skills').value.split(',').map(x=>x.trim()).filter(Boolean);await act('/api/profile',{facts,skills})}
function renderJobs(rows){document.querySelector('#jobs').innerHTML=(rows||[]).map(j=>{let p=j.payload||{};let src=p.source_name||j.source;let eligible=j.eligibility==='eligible_signal';let official=j.official_url_verified&&j.official_url;return `<tr><td><b>${esc(p.employer||j.employer_key)}</b><br>${esc(j.title)}<br>${(j.tech_matches||[]).map(t=>`<span class=pill>${esc(t)}</span>`).join('')}</td><td>${esc(j.location||'Unknown')}<br><span class=pill ${eligible?'class=good':''}>${esc(j.eligibility)}</span></td><td>${esc(j.freshness)}${j.published_at?'<br>'+esc(j.published_at.slice(0,10)):''}<br><span class=muted>${j.source_active?'source active':'unverified'}</span></td><td><a target=_blank rel=noopener href="${esc(p.source_home||'#')}">${esc(src)}</a> · <a target=_blank rel=noopener href="${esc(j.source_url)}">listing</a></td><td>${official?`<a target=_blank rel=noopener href="${esc(j.official_url)}">Official apply page</a><br><button onclick="prepare('${esc(j.id)}')">Prepare review</button>`:'<span class=muted>Official application URL not verified</span>'}</td></tr>`}).join('')||'<tr><td colspan=5 class=muted>No current matches yet. Start a discovery run or add an official ATS board.</td></tr>'}
async function prepare(id){try{let r=await req('/api/prepare/'+encodeURIComponent(id),{});notice('Review packet prepared for '+r.workflow_id);refresh()}catch(e){notice(e.message,true)}}
function renderApprovals(q){document.querySelector('#approvals').innerHTML=(q||[]).map(a=>`<div class=item><b>${esc(a.question.label)}</b> · ${esc(a.question.type)}<div>${(a.question.options||[]).map(o=>esc(o.label||o.value)).join(' / ')}</div><input id="answer-${esc(a.question_key)}" placeholder="Your truthful answer"><button onclick="approve('${esc(a.question_key)}')">Approve reusable answer</button></div>`).join('')||'<p class=muted>No pending questions.</p>'}
async function approve(k){let answer=document.querySelector('#answer-'+CSS.escape(k)).value;await act('/api/approval',{question_key:k,answer})}
function renderApps(ws){document.querySelector('#apps').innerHTML=(ws||[]).map(w=>`<div class=item><b>${esc(w.employer)}</b> · <span class=pill>${esc(w.state)}</span><br><a href="${esc(w.url)}" target=_blank rel=noopener>Open application</a> · <code>${esc(w.id)}</code><br>${w.state==='ready'||w.state==='human_required'?`<button class=secondary onclick="manualSubmit('${esc(w.id)}')">I submitted manually</button>`:''}<div class=row><input id="note-${esc(w.id)}" placeholder="Timeline note"><button class=secondary onclick="logFor('${esc(w.id)}')">Add interview/recruiter note</button></div></div>`).join('')||'<p class=muted>No application workflows yet.</p>'}
async function manualSubmit(id){let note=prompt('Confirm you personally submitted this application. This records a self-report only.');if(note)await act('/api/manual-submission',{workflow_id:id,note})}
async function logFor(id){let note=document.querySelector('#note-'+CSS.escape(id)).value;document.querySelector('#eventWorkflow').value=id;document.querySelector('#eventNote').value=note;await addEvent()}
async function addEvent(){try{await act('/api/event',{workflow_id:document.querySelector('#eventWorkflow').value,kind:document.querySelector('#eventKind').value,note:document.querySelector('#eventNote').value})}catch(e){notice(e.message,true)}}
function renderEvents(es){document.querySelector('#events').innerHTML=(es||[]).slice(0,30).map(e=>`<div class=item>${esc(e.kind)} · ${new Date(e.occurred*1000).toLocaleString()}<br>${esc(e.note)}<br><span class=muted>${esc(e.workflow_id)}</span></div>`).join('')||'<p class=muted>No timeline events.</p>'}
async function addBoard(){await act('/api/ats-board',{source:document.querySelector('#atsSource').value,board:document.querySelector('#atsBoard').value.trim()})}
setInterval(refresh,5000);refresh();
</script></body></html>"""


def make_handler(service):
    class Handler(BaseHTTPRequestHandler):
        server_version = "JobIntelligenceLocal/0.1"

        def log_message(self, fmt, *args):
            # Avoid writing URLs, candidate facts, or posted form data to logs.
            return

        def _json(self, value, status=200):
            body = json.dumps(value, ensure_ascii=False).encode()
            self.send_response(status); self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body))); self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff"); self.end_headers(); self.wfile.write(body)

        def _body(self, cap=2 * 1024 * 1024):
            size = int(self.headers.get("Content-Length", "0"))
            if size < 0 or size > cap:
                raise ValueError("request body too large")
            raw = self.rfile.read(size)
            if self.headers.get("Content-Type", "").startswith("application/json"):
                return json.loads(raw or b"{}")
            return raw

        def do_GET(self):
            host = self.headers.get("Host", "").lower()
            if not (host.startswith("127.0.0.1:") or host.startswith("localhost:") or host.startswith("[::1]:")):
                return self._json({"error": "local Host header required"}, 403)
            if self.path == "/":
                body = PAGE.replace("__CSRF__", service.csrf_token).encode(); self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body))); self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; img-src 'self' data:")
                self.send_header("X-Content-Type-Options", "nosniff"); self.send_header("Referrer-Policy", "no-referrer"); self.end_headers(); self.wfile.write(body); return
            if self.path == "/api/state":
                return self._json(service.state())
            return self._json({"error": "not found"}, 404)

        def do_POST(self):
            try:
                host = self.headers.get("Host", "").lower()
                if not (host.startswith("127.0.0.1:") or host.startswith("localhost:") or host.startswith("[::1]:")):
                    return self._json({"error": "local Host header required"}, 403)
                origin = self.headers.get("Origin")
                if origin and urlsplit(origin).netloc.lower() != host:
                    return self._json({"error": "same-origin request required"}, 403)
                if self.headers.get("X-CSRF-Token") != service.csrf_token:
                    return self._json({"error": "CSRF token missing or invalid; reload the dashboard"}, 403)
                multipart = self.headers.get("Content-Type", "").startswith("multipart/form-data")
                body = self._body(16 * 1024 * 1024 if multipart else 2 * 1024 * 1024)
                payload = body if multipart else (body if isinstance(body, dict) else {})
                path = urlsplit(self.path).path
                if path == "/api/discovery/start":
                    ok = service.start_discovery(recurring=False)
                    return self._json({"message": "Discovery started" if ok else "Discovery is already running"}, 202)
                if path == "/api/discovery/start-daily":
                    ok = service.start_discovery(recurring=True)
                    return self._json({"message": "Daily discovery worker started" if ok else "Discovery is already running"}, 202)
                if path == "/api/discovery/pause": service.pause(); return self._json({"message": "Pause requested; current bounded request will finish"})
                if path == "/api/discovery/stop": service.stop(); return self._json({"message": "Stop requested; current bounded request will finish"})
                if path == "/api/resume":
                    if not multipart:
                        raise ValueError("multipart resume upload required")
                    from email.parser import BytesParser
                    from email.policy import default
                    msg = BytesParser(policy=default).parsebytes((b"Content-Type: " + self.headers["Content-Type"].encode() + b"\r\nMIME-Version: 1.0\r\n\r\n") + payload)
                    part = next((p for p in msg.iter_parts() if p.get_param("name", header="content-disposition") == "resume"), None)
                    if not part: raise ValueError("resume file missing")
                    result = service.import_resume(part.get_filename() or "resume.pdf", part.get_payload(decode=True) or b"")
                    return self._json(result, 201)
                if path == "/api/profile":
                    return self._json({"message": "Candidate profile saved", "profile": service.approve_facts(payload.get("facts", {}), payload.get("skills"))})
                if path == "/api/ats-board":
                    service.set_ats_board(payload.get("source", ""), payload.get("board", ""))
                    return self._json({"message": "Official ATS source added"})
                if path == "/api/prepare/" or path.startswith("/api/prepare/"):
                    result = service.prepare(unquote(path.rsplit("/", 1)[-1]))
                    return self._json(result, 201)
                if path == "/api/approval":
                    service.workflows.approve_answer(payload.get("question_key", ""), payload.get("answer"))
                    return self._json({"message": "Answer approved for reuse"})
                if path == "/api/manual-submission":
                    service.record_manual_submission(payload.get("workflow_id", ""), str(payload.get("note", "")))
                    return self._json({"message": "Saved as a self-reported, unconfirmed submission"})
                if path == "/api/event":
                    service.discovery.record_event(payload.get("workflow_id", ""), payload.get("kind", "note"), payload.get("note", ""))
                    return self._json({"message": "Timeline event saved"})
                return self._json({"error": "not found"}, 404)
            except (ValueError, KeyError, json.JSONDecodeError) as exc:
                return self._json({"error": str(exc)}, 400)
            except Exception as exc:
                return self._json({"error": type(exc).__name__}, 500)

    return Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", default=os.environ.get("JOB_INTEL_HOME", "~/.job-intelligence"))
    parser.add_argument("--host", default="127.0.0.1", help="must be loopback")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--open", action="store_true", help="open the local dashboard in your browser")
    args = parser.parse_args()
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        parser.error("dashboard binds only to loopback for candidate-data protection")
    service = ProductService(args.data_dir)
    server = ThreadingHTTPServer((args.host, args.port), make_handler(service))
    print(f"Job Intelligence is ready at http://{args.host}:{server.server_address[1]}/ (data: {service.data_dir})")
    if args.open:
        webbrowser.open(f"http://{args.host}:{server.server_address[1]}/")
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        service.stop(); server.shutdown()


if __name__ == "__main__":
    main()
