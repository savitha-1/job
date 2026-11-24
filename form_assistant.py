"""Browser-assisted form review and preparation; never submits a form."""
import asyncio
import re
from urllib.parse import urlsplit

from application_planner import plan
from network_guard import PublicHTTPSProxy, origin, public_url
from web_crawler import _allowed_by_robots

GATE = re.compile(r"captcha|verify you are human|sign in|log in to continue|access denied", re.I)


def _allowed(url, fixture=False):
    p = urlsplit(url)
    if fixture and p.scheme == "http" and p.hostname in ("localhost", "127.0.0.1") and not p.username and not p.password:
        return True
    return public_url(url)


async def _prepare_form(url, profile, resume_path=None, *, dry_run=True, fixture=False, automation_permitted=False, authorize_resume_upload=False, permitted_origins=(), proxy_url=None, workflow=None, workflow_id=None, max_steps=8):
    """Map candidate-approved facts onto a form and return a review packet.

    No submit controls are clicked. Resume upload requires a separate explicit
    authorization flag; uploaded bytes are sent only to the requested origin.
    """
    if not _allowed(url, fixture):
        raise ValueError("form preparation requires HTTPS (or a localhost fixture)")
    if max_steps < 1 or max_steps > 20:
        raise ValueError("max_steps must be between 1 and 20")
    if workflow and not workflow_id:
        raise ValueError("workflow_id is required with a persistent workflow")
    if workflow:
        control = workflow.control()
        if control.get("stopped") or control.get("paused"):
            reason = "workflow_stopped" if control.get("stopped") else "workflow_paused"
            return {"url": url, "actions": [], "completed": [], "review": [{"reason": reason}], "ready": False, "submission_authorized": False}
    if not dry_run and not fixture and not automation_permitted:
        return {"url": url, "actions": [], "completed": [], "review": [{"reason": "automation_permission_not_confirmed"}], "ready": False, "submission_authorized": False}
    if not _allowed_by_robots(url, "OpportunityResearchBot/0.1", {}, proxy_url):
        return {"url": url, "actions": [], "completed": [], "review": [{"reason": "robots_disallow_or_unavailable"}], "ready": False, "submission_authorized": False}
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise RuntimeError("install Playwright and Chromium for browser form preparation") from exc
    facts = profile.get("facts", {})
    approved = {k: v.get("value") for k, v in facts.items() if isinstance(v, dict) and v.get("approved") is True}
    allowed_origins = {origin(url)}
    for approved_origin in permitted_origins:
        if not public_url(approved_origin.rstrip("/") + "/"):
            return {"url": url, "actions": [], "completed": [], "review": [{"reason": "invalid_permitted_origin"}], "ready": False, "submission_authorized": False}
        if not _allowed_by_robots(approved_origin.rstrip("/") + "/", "OpportunityResearchBot/0.1", {}, proxy_url):
            return {"url": url, "actions": [], "completed": [], "review": [{"reason": "permitted_origin_robots_disallow_or_unavailable", "origin": origin(approved_origin)}], "ready": False, "submission_authorized": False}
        allowed_origins.add(origin(approved_origin))
    if resume_path and authorize_resume_upload:
        approved["resume"] = str(resume_path)
    all_steps, all_actions, completed = [], [], []
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, proxy={"server": proxy_url} if proxy_url else None, args=["--disable-quic", "--disable-background-networking", "--disable-component-update", "--dns-prefetch-disable", "--disable-features=AsyncDns,DnsOverHttps"] if proxy_url else [])
        context = await browser.new_context(service_workers="block")
        page = await context.new_page()
        async def guard(route):
            # Preparation is read-only at the HTTP layer. A scripted Next
            # control must never smuggle a POST/PUT through the browser.
            if route.request.method in ("GET", "HEAD") and _allowed(route.request.url, fixture) and origin(route.request.url) in allowed_origins:
                await route.continue_()
            else:
                await route.abort("blockedbyclient")
        await context.route("**/*", guard)
        try:
            current_url = url
            review = []
            status = None
            for step_index in range(max_steps):
                if workflow:
                    control = workflow.control()
                    if control.get("stopped") or control.get("paused"):
                        reason = "workflow_stopped" if control.get("stopped") else "workflow_paused"
                        current = workflow.get(workflow_id)
                        if current and current["state"] == "preparing":
                            workflow.transition(workflow_id, "preparing", "paused", failure=reason)
                        review.append({"reason": reason})
                        break
                if workflow:
                    current = workflow.get(workflow_id)
                    if current and current["state"] in ("review", "human_required", "ready", "failed", "paused"):
                        workflow.transition(workflow_id, current["state"], "preparing")
                if current_url is not None:
                    response = await page.goto(current_url, wait_until="domcontentloaded", timeout=15000)
                    if not response or response.status >= 400:
                        review = [{"reason": "page_load_failed"}]
                        break
                    status = response.status
                try:
                    await page.wait_for_load_state("networkidle", timeout=1200)
                except Exception:
                    pass
                text = await page.locator("body").inner_text()
                gate_elements = await page.locator('iframe[src*="captcha" i], [id*="captcha" i], [class*="captcha" i], input[name*="captcha" i], input[type="password"]').count()
                if gate_elements or GATE.search(text):
                    review = [{"reason": "human_verification_or_access_gate", "step": step_index}]
                    if workflow:
                        workflow.record_step(workflow_id, step_index, page.url, [], status="human_required")
                        workflow.transition(workflow_id, "preparing", "human_required")
                    break
                fields = await page.locator("input,textarea,select").evaluate_all("""els => {
                  const all=Array.from(els); const out=[]; const seen=new Set();
                  all.forEach((e,i)=>{
                    if(e.disabled || e.type==='hidden' || ['submit','button','reset','image'].includes(e.type)) return;
                    const label=e.labels&&e.labels[0];
                    let options=[]; let group_indices=[]; let fieldLabel=label?.innerText||e.getAttribute('aria-label')||e.getAttribute('name')||'';
                    if(e.tagName==='SELECT') options=Array.from(e.options).filter(o=>!o.disabled).map(o=>({value:o.value,label:(o.label||o.textContent||'').trim()}));
                    if(e.type==='radio') {
                      const group=all.map((x,j)=>({x,j})).filter(z=>z.x.type==='radio'&&z.x.name===e.name);
                      const key=e.form?.id+':'+e.name;
                      if(seen.has(key)) return; seen.add(key);
                      group_indices=group.map(z=>z.j);
                      options=group.map(z=>({value:z.x.value,label:(z.x.labels?.[0]?.innerText||z.x.getAttribute('aria-label')||z.x.value).trim()}));
                      const legend=e.closest('fieldset')?.querySelector('legend')?.innerText;
                      fieldLabel=legend||e.getAttribute('name')||fieldLabel;
                    }
                    out.push({id:String(i),label:fieldLabel,name:e.getAttribute('name')||'',placeholder:e.getAttribute('placeholder')||'',type:(e.type||e.tagName.toLowerCase()),required:!!e.required,disabled:!!e.disabled,options,group_indices});
                  }); return out;
                }""")
                result = workflow.plan_step(workflow_id, step_index, fields, approved, page.url) if workflow else plan(fields, approved)
                pending = []
                if workflow:
                    result, pending = result
                    if pending:
                        review.extend({"id": x["field_id"], "reason": "human_approval_required", "question_key": x["question_key"]} for x in pending)
                safe_actions = [{k: a[k] for k in ("id", "operation", "profile_key") if k in a} for a in result["actions"]]
                all_actions.extend(safe_actions)
                if not dry_run:
                    field_locator = page.locator("input,textarea,select")
                    for action in result["actions"]:
                        if workflow:
                            control = workflow.control()
                            if control.get("stopped") or control.get("paused"):
                                reason = "workflow_stopped" if control.get("stopped") else "workflow_paused"
                                review.append({"reason": reason})
                                break
                        index = int(action["id"])
                        locator = field_locator.nth(index)
                        value = action.get("value", approved.get(action.get("profile_key")))
                        try:
                            if action["operation"] == "upload":
                                if not authorize_resume_upload:
                                    review.append({"id": action["id"], "reason": "resume_upload_requires_explicit_authorization"})
                                    continue
                                await locator.set_input_files(str(value))
                            elif action["operation"] == "select":
                                await locator.select_option(value=value)
                            elif action["operation"] == "check" and fields[index].get("type") == "radio":
                                group = fields[index].get("group_indices") or [index]
                                choice = None
                                for radio_index in group:
                                    radio = field_locator.nth(int(radio_index))
                                    if await radio.get_attribute("value") == str(value):
                                        choice = radio
                                        break
                                if choice is None:
                                    raise ValueError("radio choice disappeared")
                                await choice.check()
                            elif action["operation"] in ("check", "uncheck"):
                                if action["operation"] == "check": await locator.check()
                                else: await locator.uncheck()
                            else:
                                await locator.fill(str(value))
                            completed.append({"step": step_index, "id": action["id"], "operation": action["operation"], "profile_key": action.get("profile_key")})
                        except Exception:
                            review.append({"id": action["id"], "reason": "field_interaction_failed"})
                review.extend(result["review"])
                all_steps.append({"url": page.url, "status": status, "fields": fields, "step": step_index})
                if review:
                    break
                validation = await page.locator('[aria-invalid="true"], [role="alert"]').evaluate_all("els=>els.filter(e=>e.offsetParent!==null).map(e=>(e.innerText||e.getAttribute('aria-label')||'').trim()).filter(Boolean).slice(0,10)")
                if validation:
                    review.extend({"reason": "portal_validation", "message": msg[:300]} for msg in validation)
                    break
                if dry_run or not automation_permitted:
                    break
                if workflow:
                    control = workflow.control()
                    if control.get("stopped") or control.get("paused"):
                        reason = "workflow_stopped" if control.get("stopped") else "workflow_paused"
                        review.append({"reason": reason})
                        break
                next_items = await page.locator("a[href],button,input[type=button]").evaluate_all("els=>els.map((e,i)=>({i,text:(e.innerText||e.value||e.getAttribute('aria-label')||'').trim(),href:e.href||'',type:e.type||'',disabled:!!e.disabled}))")
                next_step = next((x for x in next_items if not x["disabled"] and re.search(r"^(next|continue|save and continue|next step|proceed)$",x["text"],re.I)), None)
                if not next_step:
                    break
                if next_step["href"]:
                    if origin(next_step["href"]) not in allowed_origins:
                        review.append({"reason": "next_step_origin_not_permitted"})
                        break
                    current_url = next_step["href"]
                    continue
                if next_step["type"] not in ("button",):
                    review.append({"reason": "next_step_may_submit_form"})
                    break
                try:
                    await page.locator("a[href],button,input[type=button]").nth(next_step["i"]).click(timeout=3000)
                    await page.wait_for_load_state("domcontentloaded", timeout=5000)
                    # Reuse the live document: a client-side step may have
                    # replaced its DOM without navigating to a new URL.
                    current_url = None
                    continue
                except Exception:
                    review.append({"reason": "next_step_failed"})
                    break
            if not review and len(all_steps) >= max_steps:
                review.append({"reason": "multi_step_limit_reached"})
            if workflow:
                current = workflow.get(workflow_id)
                if current and current["state"] == "preparing":
                    target = "human_required" if review else "ready"
                    workflow.transition(workflow_id, "preparing", target, failure=None)
            return {"url": page.url, "status": status, "steps": all_steps, "actions": all_actions, "completed": completed, "review": review, "ready": not review, "dry_run": dry_run, "submission_authorized": False, "next_step": "human_review_required" if review else "manual_review_before_submission"}
        finally:
            await context.close()
            await browser.close()


def prepare_form_sync(*args, **kwargs):
    return asyncio.run(prepare_form(*args, **kwargs))


async def prepare_form(url, profile, resume_path=None, *, dry_run=True, fixture=False, automation_permitted=False, authorize_resume_upload=False, permitted_origins=(), workflow=None, workflow_id=None, max_steps=8):
    args = (url, profile, resume_path)
    options = {"dry_run": dry_run, "fixture": fixture, "automation_permitted": automation_permitted, "authorize_resume_upload": authorize_resume_upload, "permitted_origins": permitted_origins, "workflow": workflow, "workflow_id": workflow_id, "max_steps": max_steps}
    if fixture:
        return await _prepare_form(*args, **options)
    with PublicHTTPSProxy() as proxy:
        return await _prepare_form(*args, **options, proxy_url=proxy.url)
