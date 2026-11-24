"""Browser-assisted form review and preparation; never submits a form."""
import asyncio
import ipaddress
import re
import socket
from urllib.parse import urlsplit

from application_planner import plan
from web_crawler import _allowed_by_robots

GATE = re.compile(r"captcha|verify you are human|sign in|log in to continue|access denied", re.I)


def _allowed(url, fixture=False):
    p = urlsplit(url)
    if not ((p.scheme == "https" or (fixture and p.scheme == "http" and p.hostname in ("localhost", "127.0.0.1"))) and p.hostname and not p.username and not p.password):
        return False
    try:
        address = ipaddress.ip_address(p.hostname)
    except ValueError:
        return True
    return fixture and address.is_loopback or address.is_global


async def prepare_form(url, profile, resume_path=None, *, dry_run=True, fixture=False, automation_permitted=False, authorize_resume_upload=False):
    """Map candidate-approved facts onto a form and return a review packet.

    No submit controls are clicked. Resume upload requires a separate explicit
    authorization flag; uploaded bytes are sent only to the requested origin.
    """
    if not _allowed(url, fixture):
        raise ValueError("form preparation requires HTTPS (or a localhost fixture)")
    if not dry_run and not fixture and not automation_permitted:
        return {"url": url, "actions": [], "completed": [], "review": [{"reason": "automation_permission_not_confirmed"}], "ready": False, "submission_authorized": False}
    if not _allowed_by_robots(url, "OpportunityResearchBot/0.1", {}):
        return {"url": url, "actions": [], "completed": [], "review": [{"reason": "robots_disallow_or_unavailable"}], "ready": False, "submission_authorized": False}
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise RuntimeError("install Playwright and Chromium for browser form preparation") from exc
    facts = profile.get("facts", {})
    approved = {k: v.get("value") for k, v in facts.items() if isinstance(v, dict) and v.get("approved") is True}
    if resume_path and authorize_resume_upload:
        approved["resume"] = str(resume_path)
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        async def guard(route):
            if _allowed(route.request.url, fixture):
                await route.continue_()
            else:
                await route.abort("blockedbyclient")
        await page.route("**/*", guard)
        try:
            response = await page.goto(url, wait_until="domcontentloaded", timeout=15000)
            if not response or response.status >= 400:
                return {"url": url, "status": response.status if response else None, "actions": [], "review": [{"reason": "page_load_failed"}], "submission_authorized": False}
            text = await page.locator("body").inner_text()
            gate_elements = await page.locator('iframe[src*="captcha" i], [id*="captcha" i], [class*="captcha" i], input[name*="captcha" i], input[type="password"]').count()
            if gate_elements or GATE.search(text):
                return {"url": page.url, "actions": [], "review": [{"reason": "human_verification_or_access_gate"}], "submission_authorized": False}
            fields = await page.locator("input,textarea,select").evaluate_all("els => els.map((e,i) => { const l=e.labels&&e.labels[0]; return {id:String(i),label:l?.innerText||e.getAttribute('aria-label')||e.getAttribute('name')||'',name:e.getAttribute('name')||'',placeholder:e.getAttribute('placeholder')||'',type:(e.type||e.tagName.toLowerCase()),required:!!e.required,disabled:!!e.disabled} })")
            result = plan(fields, approved)
            completed = []
            if not dry_run:
                for action in result["actions"]:
                    index = int(action["id"])
                    locator = page.locator("input,textarea,select").nth(index)
                    key = action["profile_key"]
                    value = approved.get(key)
                    try:
                        if action["operation"] == "upload":
                            await locator.set_input_files(value)
                        else:
                            await locator.fill(str(value))
                        completed.append({"id": action["id"], "operation": action["operation"], "profile_key": key})
                    except Exception:
                        result["review"].append({"id": action["id"], "reason": "field_interaction_failed"})
                result["ready"] = not result["review"]
            return {"url": page.url, "status": response.status, "fields": fields, "actions": result["actions"], "completed": completed, "review": result["review"], "ready": result["ready"], "dry_run": dry_run, "submission_authorized": False, "next_step": "human_review_required"}
        finally:
            await browser.close()


def prepare_form_sync(*args, **kwargs):
    return asyncio.run(prepare_form(*args, **kwargs))
