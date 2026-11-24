"""Bounded read-only browser exploration for public hiring pages.

Playwright is an optional dependency. This crawler never authenticates, clicks
application controls, fills fields, or submits forms. CAPTCHA/login gates stop
the crawl and are recorded as human checkpoints.
"""
import asyncio
import hashlib
import ipaddress
import re
import time
import urllib.robotparser
from collections import deque
from urllib.parse import urldefrag, urljoin, urlsplit
from urllib.request import Request, urlopen

from archive_discovery import classify
from web_evidence import inspect_html

RESTRICTED = re.compile(r"captcha|verify (?:that )?you are human|unusual traffic|access denied|sign in to continue|log in to continue", re.I)
CAREER_HINT = re.compile(r"career|hiring|job|vacanc|join us|open position|engineering blog", re.I)


def dom_fingerprint(html):
    """Stable 64-bit SimHash of visible-ish DOM text, insensitive to whitespace."""
    text = re.sub(r"(?is)<(script|style|svg|noscript)\b.*?</\1\s*>", " ", html)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip().lower()
    tokens = re.findall(r"[\w+#.-]+", text)
    weights = [0] * 64
    grams = [" ".join(tokens[i:i + 3]) for i in range(max(1, len(tokens) - 2))]
    if not grams and tokens:
        grams = tokens
    for gram in grams:
        bits = int.from_bytes(hashlib.blake2b(gram.encode(), digest_size=8).digest(), "big")
        for bit in range(64):
            weights[bit] += 1 if bits & (1 << bit) else -1
    return "".join("1" if n >= 0 else "0" for n in weights)


def fingerprint_distance(left, right):
    if len(left) != len(right):
        raise ValueError("fingerprints must have equal length")
    return sum(a != b for a, b in zip(left, right))


def _allowed_by_robots(url, user_agent, cache):
    p = urlsplit(url)
    if p.hostname in ("127.0.0.1", "localhost", "::1"):
        return True
    origin = f"{p.scheme}://{p.netloc}"
    if origin not in cache:
        parser = urllib.robotparser.RobotFileParser()
        parser.set_url(origin + "/robots.txt")
        try:
            request = Request(parser.url, headers={"User-Agent": user_agent})
            with urlopen(request, timeout=5) as response:
                parser.parse(response.read(256_000).decode("utf-8", "replace").splitlines())
        except Exception:
            # Fail closed when robots policy cannot be read.
            parser.parse(["User-agent: *", "Disallow: /"])
        cache[origin] = parser
    return cache[origin].can_fetch(user_agent, url)


def _url_allowed(url, fixture=False):
    p = urlsplit(url)
    if not ((p.scheme == "https" or (fixture and p.scheme == "http" and p.hostname in ("127.0.0.1", "localhost", "::1"))) and p.hostname and not p.username and not p.password):
        return False
    try:
        address = ipaddress.ip_address(p.hostname)
    except ValueError:
        return True
    return (fixture and address.is_loopback) or address.is_global


async def explore(seeds, store=None, max_pages=50, max_depth=2, per_host=5, delay=0.5, timeout_ms=15000, fixture=False):
    """Explore public sites by prioritized links; returns pages, edges, checkpoints."""
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise RuntimeError("install Playwright and its Chromium browser to use web exploration") from exc
    if max_pages < 1 or max_depth < 0 or per_host < 1 or delay < 0:
        raise ValueError("invalid crawl bounds")
    user_agent = "OpportunityResearchBot/0.1 (+read-only public career-page discovery)"
    queue, visited, host_count, last_host = [], set(), {}, {}
    for seed in seeds:
        if not _url_allowed(seed, fixture):
            raise ValueError("seed must be a public HTTPS URL")
        queue.append((0, 0, seed, seed))
    queue.sort(key=lambda item: (item[0], item[2]))
    robots, pages, edges, checkpoints = {}, [], [], []
    exhausted_hosts = set()
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(user_agent=user_agent, service_workers="block")
        page = await context.new_page()
        page.set_default_navigation_timeout(timeout_ms)
        if store:
            store.recover_frontier()
            for priority, depth, seed, parent in queue:
                store.enqueue(seed, priority=100-priority, depth=depth, parent=parent, allow_local_fixture=fixture)
            queue.clear()
        try:
            while (queue or store) and len(pages) < max_pages:
                if store:
                    batch = store.take_records(limit=1, per_host=per_host, exclude_hosts=exhausted_hosts)
                    if not batch:
                        break
                    work = batch[0]
                    url, depth, parent = work["url"], work["depth"], work["parent"] or work["url"]
                    priority = 0
                    attempts = work.get("attempts", 1)
                else:
                    queue.sort(key=lambda item: (item[0], item[2]))
                    priority, depth, url, parent = queue.pop(0)
                    attempts = 1
                url = urldefrag(url)[0]
                if url in visited:
                    continue
                visited.add(url)
                host = urlsplit(url).hostname.lower()
                if depth > max_depth:
                    if store:
                        store.finish(url, True)
                    continue
                if host_count.get(host, 0) >= per_host:
                    if store:
                        store.defer(url)
                        exhausted_hosts.add(host)
                    continue
                if store and not store.reserve_host_request(host, max_requests=max(per_host, 1)):
                    store.defer(url)
                    exhausted_hosts.add(host)
                    continue
                if not _allowed_by_robots(url, user_agent, robots):
                    checkpoints.append({"url": url, "reason": "robots_disallow"})
                    if store:
                        store.finish(url, True)
                    continue
                wait = delay - (time.monotonic() - last_host.get(host, 0))
                if wait > 0:
                    await asyncio.sleep(wait)
                last_host[host] = time.monotonic()
                host_count[host] = host_count.get(host, 0) + 1
                try:
                    page_host = urlsplit(url).hostname.lower()
                    async def guard(route):
                        target = urlsplit(route.request.url)
                        # Same-origin resources only; this bounds third-party
                        # requests and prevents a page from probing local hosts.
                        if _url_allowed(route.request.url, fixture) and target.hostname.lower() == page_host:
                            await route.continue_()
                        else:
                            await route.abort("blockedbyclient")
                    await page.unroute("**/*")
                    await page.route("**/*", guard)
                    response = await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                    if response is None:
                        continue
                    try:
                        await page.wait_for_load_state("networkidle", timeout=min(timeout_ms, 1500))
                    except Exception:
                        # Keep exploring long-polling pages after the bounded settle window.
                        pass
                    status = response.status
                    final_url = page.url
                    if not _url_allowed(final_url, fixture):
                        checkpoints.append({"url": url, "reason": "redirect_out_of_scope"})
                        continue
                    html = await page.content()
                    title = await page.title()
                    body = await page.locator("body").inner_text(timeout=2000)
                    gate_elements = await page.locator('iframe[src*="captcha" i], [id*="captcha" i], [class*="captcha" i], input[name*="captcha" i], input[type="password"]').count()
                    links = await page.locator("a[href]").evaluate_all("els => els.map(a => ({href:a.href, text:(a.innerText||a.getAttribute('aria-label')||'').trim()}))")
                except Exception as exc:
                    checkpoints.append({"url": url, "reason": "navigation_error", "detail": type(exc).__name__})
                    if store:
                        store.finish(url, False)
                    if attempts < 3:
                        await asyncio.sleep(min(2 ** (attempts - 1), 8))
                    continue
                if gate_elements or RESTRICTED.search(final_url + " " + title + " " + body[:10000]):
                    checkpoints.append({"url": final_url, "reason": "human_verification_or_access_gate"})
                    if store:
                        store.finish(url, True)
                        store.checkpoint("human_gate:" + final_url, checkpoints[-1])
                    continue
                fp = dom_fingerprint(html)
                structured = inspect_html(final_url, html)
                tech = [name for name in ("Go", "Python", "TypeScript", "Node.js", "React", "Kubernetes", "Java", "Rust", "PostgreSQL", "AWS") if re.search(r"(?<!\w)" + re.escape(name) + r"(?!\w)", body, re.I)]
                row = {"url": final_url, "requested_url": url, "parent": parent, "status": status, "title": title, "fingerprint": fp, "depth": depth, "career_page": bool(classify(final_url)), "job_postings": structured["job_postings"], "career_links": structured["career_links"], "technologies": tech, "hiring_signal": bool(structured["job_postings"] or re.search(r"we.re hiring|join our team|open roles|careers", body, re.I)), "observed_at": time.time()}
                pages.append(row)
                edges.append({"from": parent, "to": final_url, "kind": "navigation" if parent != url else "seed"})
                if store:
                    store.record_page(row)
                    store.finish(url, True)
                    if parent != url:
                        store.record_edge(parent, final_url, "navigation")
                    store.checkpoint("crawler:last", {"url": final_url, "depth": depth})
                    host_id = store.add_employer(host, title if row["career_page"] else None)
                    if row["career_page"]:
                        store.save_company_lead({"organization": host, "host": host, "verified_employer": False, "evidence": [{"url": final_url, "kind": "live_career_page", "status": status}]}, "live_career_page")
                    for index, posting in enumerate(structured["job_postings"]):
                        posting_url = posting.get("application_url") or final_url
                        job_id = posting_url or f"{host}:{index}:{title}"
                        store.save_job(host_id, {"source": "jsonld", "board": host, "id": job_id, "title": posting.get("title", ""), "location": posting.get("applicant_location") or posting.get("location") or "", "description": "", "application_url": posting_url, "evidence_url": final_url, "observed_at": row["observed_at"]})
                if depth >= max_depth or status < 200 or status >= 400:
                    continue
                for link in links[:500]:
                    target = urldefrag(link.get("href", ""))[0]
                    if not _url_allowed(target, fixture):
                        continue
                    same = urlsplit(target).hostname.lower() == host
                    # Cross-domain traversal only for high-confidence career/employer links.
                    hint = link.get("text", "") + " " + target
                    if not same and not (classify(target) or CAREER_HINT.search(hint)):
                        continue
                    rank = 0 if classify(target) else (1 if CAREER_HINT.search(hint) else 2)
                    if store:
                        store.enqueue(target, priority=100 - 10 * depth - rank, depth=depth + 1, parent=final_url, allow_local_fixture=fixture)
                    else:
                        queue.append((rank, depth + 1, target, final_url))
        finally:
            await context.close()
            await browser.close()
    return {"pages": pages, "edges": edges, "checkpoints": checkpoints, "unique_states": len({p["fingerprint"] for p in pages})}


def explore_sync(*args, **kwargs):
    return asyncio.run(explore(*args, **kwargs))
