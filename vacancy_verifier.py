"""Evidence-backed public vacancy verification using static, ATS and browser paths."""
import datetime as dt
import html
import json
import re
import time
from difflib import SequenceMatcher
from html.parser import HTMLParser
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request

from discover import detect_public_ats, fetch_public_ats
from engine import eligibility_evidence
from network_guard import PublicHTTPSProxy, build_public_opener, origin, public_url
from relevance import score_job
from web_crawler import _allowed_by_robots, explore_sync
from web_evidence import inspect_html

GATE = re.compile(r"captcha|verify (?:that )?you are human|unusual traffic|access denied|sign in to continue|log in to continue", re.I)
CLOSED = re.compile(r"applications? (?:are )?closed|position (?:is )?filled|no longer accepting applications|job (?:is )?no longer available|vacancy has expired", re.I)
CAREER_LINK = re.compile(r"career|jobs?|vacanc|join our team|open position", re.I)
APPLY_LINK = re.compile(r"apply(?:\s+now|\s+for\s+(?:this\s+)?(?:job|position))?|application|submit application", re.I)
STOP = {"inc", "llc", "ltd", "limited", "corp", "corporation", "company", "co", "jobs", "careers", "the", "and", "group", "technologies", "com", "org", "net", "io", "dev", "ai", "example", "www", "boards", "greenhouse", "lever", "ashbyhq", "workable", "smartrecruiters", "eu", "de", "software", "technology", "solutions"}


class _RedirectSameOrigin(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if origin(req.full_url) != origin(newurl):
            raise HTTPError(req.full_url, code, "cross-origin redirect blocked", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class _VisiblePage(HTMLParser):
    def __init__(self, base):
        super().__init__(convert_charrefs=True)
        self.base = base
        self.text = []
        self.headings = []
        self.links = []
        self.meta = {}
        self._heading = None
        self._anchor = None
        self._button = None
        self.forms = 0

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in ("h1", "h2", "title"):
            self._heading = [tag, []]
        if tag == "a":
            self._anchor = [urljoin(self.base, attrs.get("href", "")), []]
        if tag == "button":
            self._button = []
        if tag == "form":
            self.forms += 1
        if tag == "meta":
            key = (attrs.get("property") or attrs.get("name") or "").casefold()
            if key in ("og:site_name", "og:title", "application-name"):
                self.meta[key] = attrs.get("content", "")[:300]

    def handle_data(self, data):
        value = re.sub(r"\s+", " ", data).strip()
        if not value:
            return
        self.text.append(value)
        if self._heading is not None:
            self._heading[1].append(value)
        if self._anchor is not None:
            self._anchor[1].append(value)
        if self._button is not None:
            self._button.append(value)

    def handle_endtag(self, tag):
        if self._heading and self._heading[0] == tag:
            self.headings.append(" ".join(self._heading[1])[:300])
            self._heading = None
        if tag == "a" and self._anchor is not None:
            url, text = self._anchor
            self.links.append({"url": url, "text": " ".join(text)[:200]})
            self._anchor = None
        if tag == "button" and self._button is not None:
            self.links.append({"url": "", "text": " ".join(self._button)[:200]})
            self._button = None


def _tokens(value):
    aliases = {"sr": "senior", "jr": "junior", "swe": "software engineer", "dev": "developer"}
    words = re.findall(r"[a-z0-9+#]+", str(value or "").casefold())
    words = [aliases.get(word, word) for word in words]
    return {part for word in words for part in word.split() if part not in STOP}


def title_similarity(expected, observed):
    a, b = _tokens(expected), _tokens(observed)
    if not a or not b:
        return 0.0
    overlap = len(a & b) / max(1, min(len(a), len(b)))
    ratio = SequenceMatcher(None, " ".join(sorted(a)), " ".join(sorted(b))).ratio()
    role_words = {"engineer", "developer", "programmer", "architect", "scientist", "sre", "devops", "designer"}
    shared_role = bool((a & b) & role_words)
    return max(ratio, overlap * 0.88) if shared_role else 0.0


def employer_similarity(expected, observed):
    a, b = _tokens(expected), _tokens(observed)
    if not a or not b:
        return 0.0
    if len(a) == 1 and next(iter(a)) in {"startup", "startups", "company", "software", "tech", "technology", "team", "engineering"}:
        return 0.0
    if a <= b or b <= a:
        return 0.95
    return len(a & b) / max(len(a), len(b))


def _active(valid_through=None):
    if not valid_through:
        return True
    try:
        value = dt.datetime.fromisoformat(str(valid_through).replace("Z", "+00:00"))
        if value.tzinfo is None:
            value = value.replace(tzinfo=dt.timezone.utc)
        return value >= dt.datetime.now(dt.timezone.utc)
    except ValueError:
        return False


def _identity_evidence(url, page, employer, org_names=(), board=None):
    evidence = []
    for name in org_names:
        score = employer_similarity(employer, name)
        if score >= 0.8:
            evidence.append({"kind": "structured_organization", "value": name, "score": round(score, 3)})
    for name in page.meta.values():
        score = employer_similarity(employer, name)
        if score >= 0.8:
            evidence.append({"kind": "site_metadata", "value": name, "score": round(score, 3)})
    host = (urlsplit(url).hostname or "").casefold()
    host_score = employer_similarity(employer, host.replace(".", " ").replace("-", " "))
    if host_score >= 0.8:
        evidence.append({"kind": "employer_domain_name_match", "value": host, "score": round(host_score, 3)})
    if board:
        score = employer_similarity(employer, board)
        if score >= 0.8:
            evidence.append({"kind": "ats_board_identifier_match", "value": board, "score": round(score, 3)})
    return evidence


def _application_is_job_specific(vacancy_url, application_url):
    if not application_url or not public_url(application_url):
        return False
    app_parts = [part.casefold() for part in urlsplit(application_url).path.split("/") if part]
    generic = {"apply", "application", "submit", "jobs", "job", "careers", "career", "job-openings"}
    specific = any(part not in generic and not part.isdigit() for part in app_parts)
    vacancy_parts = [part.casefold() for part in urlsplit(vacancy_url).path.split("/") if part]
    requisition = vacancy_parts[-1] if vacancy_parts else ""
    carries_id = bool(requisition and requisition in urlsplit(application_url).path.casefold())
    same_origin = origin(application_url) == origin(vacancy_url)
    ats_form = bool(detect_public_ats(vacancy_url) and same_origin and
                    (application_url.rstrip("/") == vacancy_url.rstrip("/") or carries_id))
    return (specific or ats_form) and (same_origin or carries_id)


def _verified_record(target, *, title, description, location, vacancy_url, application_url, evidence_url, identity, active_evidence, method, requisition_id, observed_at=None):
    from relevance import score_job
    description = re.sub(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", "[email redacted]", str(description), flags=re.I)
    description = re.sub(r"(?<!\w)(?:\+?\d[\d ()-]{7,}\d)(?!\w)", "[phone redacted]", description)
    eligibility, eligibility_details = eligibility_evidence(location, description, title=title)
    relevance = score_job({"title": title, "description": description}, target.get("skills") or ["Go", "Python", "TypeScript", "Node.js", "React", "Kubernetes", "PostgreSQL", "AWS"])
    return {"verified": True, "employer": target["employer"], "employer_identity_evidence": identity,
        "title": title, "requisition_id": requisition_id, "vacancy_url": vacancy_url,
        "application_url": application_url, "description": description[:16000], "location": location or "Unknown",
        "india_eligibility": eligibility, "eligibility_evidence": eligibility_details,
        "technical_relevance": relevance, "active_evidence": active_evidence, "method": method,
        "evidence_url": evidence_url, "observed_at": observed_at or dt.datetime.now(dt.timezone.utc).isoformat()}


def _posting_match(target, posting, current_url, method, board=None, visible_page=None):
    title = str(posting.get("title") or posting.get("name") or "")
    similarity = title_similarity(target.get("title"), title)
    if similarity < 0.52:
        return None, "title_unmatched"
    org = posting.get("organization") or posting.get("company") or {}
    org_name = (org.get("name") if isinstance(org, dict) else str(org or "")) or ""
    page = visible_page or _VisiblePage(current_url)
    identity = _identity_evidence(current_url, page, target.get("employer", ""), [org_name] if org_name else [], board=board)
    if not identity:
        return None, "employer_identity_unverified"
    valid_through = posting.get("valid_through") or posting.get("validThrough")
    if not _active(valid_through):
        return None, "vacancy_expired_or_invalid"
    page_text = " ".join(visible_page.text) if visible_page else ""
    description_text = str(posting.get("description") or posting.get("descriptionPlain") or "")
    if CLOSED.search(page_text + " " + description_text):
        return None, "vacancy_closed"
    application = posting.get("application_url") or posting.get("applicationUrl") or posting.get("applyUrl")
    vacancy = posting.get("vacancy_url") or posting.get("url") or current_url
    if not application:
        application = posting.get("direct_apply_url")
    if not application and visible_page:
        links=[x for x in visible_page.links if APPLY_LINK.search(x.get("text", "")) and x.get("url") and public_url(x["url"])]
        if links:
            application=links[0]["url"]
    if not application and detect_public_ats(current_url) and visible_page and visible_page.forms:
        application=current_url
    if not application or not public_url(application):
        return None, "application_link_missing"
    if not _application_is_job_specific(vacancy, application):
        return None, "application_link_not_job_specific"
    location = posting.get("location") or posting.get("applicant_location") or "Unknown"
    if not isinstance(location, str):
        location = json.dumps(location, ensure_ascii=False)
    description = description_text
    job_id = str(posting.get("id") or posting.get("source_id") or urlsplit(vacancy).path.rstrip("/").split("/")[-1])
    evidence = {"kind": "current_public_ats_posting" if "api" in method else "schema_org_jobposting",
                "title_similarity": round(similarity, 3), "organization": org_name or None, "valid_through": valid_through,
                "application_url": application}
    return _verified_record(target,title=title,description=description,location=location,vacancy_url=vacancy,
        application_url=application,evidence_url=posting.get("evidence_url") or current_url,identity=identity,
        active_evidence=evidence,method=method,requisition_id=method.split(":")[-1]+":"+job_id), None


def _static_page(url, *, timeout, max_bytes, robots_cache):
    robots_new = origin(url) not in robots_cache
    if not _allowed_by_robots(url, "LocalJobResearch/0.2", robots_cache):
        return {"error":"robots_denied", "request_count": int(robots_new), "bytes_read":0}
    request = Request(url,headers={"User-Agent":"LocalJobResearch/0.2 (read-only vacancy verification)","Accept":"text/html,application/xhtml+xml"})
    with PublicHTTPSProxy() as proxy:
        opener=build_public_opener(proxy.url); opener.add_handler(_RedirectSameOrigin())
        with opener.open(request,timeout=timeout) as response:
            if response.status != 200:
                return {"error":"http_status_"+str(response.status),"status":response.status,"request_count":1 + int(robots_new),"bytes_read":0}
            ctype=response.headers.get("Content-Type","").casefold()
            if "html" not in ctype and "text/" not in ctype:
                return {"error":"non_html_response","status":response.status,"request_count":1 + int(robots_new),"bytes_read":0}
            body=response.read(max_bytes+1)
            final=response.geturl()
    if len(body)>max_bytes:
        return {"error":"response_size_limit","status":200,"request_count":1 + int(robots_new),"bytes_read":max_bytes+1}
    html_doc=body.decode("utf-8","replace")
    visible=_VisiblePage(final); visible.feed(html_doc); visible.close()
    structured=inspect_html(final,html_doc)
    return {"url":final,"html":html_doc,"text":" ".join(visible.text)[:50000],"page":visible,
        "structured":structured,"status":200,"request_count":1 + int(robots_new),"bytes_read":len(body)}


def _static_match(target, page, ats=None):
    url=page["url"]
    for posting in page["structured"]["job_postings"]:
        result, reason=_posting_match(target,posting,url,"static:jsonld",visible_page=page["page"])
        if result:
            return result,reason
    ats_info=ats or detect_public_ats(url)
    board=ats_info.get("board") if ats_info else None
    structured_names=[str(x.get("name")) for x in page["structured"].get("organizations",[]) if x.get("name")]
    identity=_identity_evidence(url,page["page"],target.get("employer", ""),structured_names,board=board)
    candidates=[(heading,title_similarity(target.get("title"),heading)) for heading in page["page"].headings]
    candidates.extend((title,title_similarity(target.get("title"),title)) for title in (page["page"].meta.get("og:title"),))
    best_title,score=max(candidates,key=lambda x:x[1],default=("",0))
    if not identity:
        return None,"employer_identity_unverified"
    visible_body = str(page.get("text", "")) + " " + " ".join(page["page"].headings)
    if CLOSED.search(visible_body):
        return None, "vacancy_closed"
    if score<0.52:
        return None,"vacancy_not_found" if not page["page"].headings else "title_unmatched"
    apply_links=[x for x in page["page"].links if APPLY_LINK.search(x["text"]) and x.get("url") and public_url(x["url"])]
    if ats_info and ats_info.get("source") in {"greenhouse","lever","lever-eu","ashby","smartrecruiters","workable","personio"} and re.search(r"jobs?|postings?|openings?",urlsplit(url).path,re.I):
        application=url
    elif apply_links:
        application=apply_links[0]["url"]
    elif page["page"].forms and ats_info and ats_info.get("source") in {"greenhouse","lever","lever-eu","ashby","workable","smartrecruiters"}:
        application=url
    else:
        return None,"application_link_missing"
    if not _application_is_job_specific(url, application):
        return None,"application_link_not_job_specific"
    title=best_title
    description=page["text"]
    # Current accessible vacancy page plus a job-specific application control
    # is live evidence even when the site omits JSON-LD.
    if GATE.search(description[:10000]):
        return None,"human_verification_gate"
    location=_extract_location(description)
    requisition=urlsplit(url).path.rstrip("/").split("/")[-1] or hashlib_path(url)
    active={"kind":"visible_job_page_and_apply_control","title_similarity":round(score,3),"apply_control":apply_links[0]["text"] if apply_links else "form","observed_http_status":page["status"]}
    return _verified_record(target,title=title,description=description,location=location,vacancy_url=url,
        application_url=application,evidence_url=url,identity=identity,active_evidence=active,
        method="static:visible_html",requisition_id=ats_info["source"]+":"+requisition if ats_info else "web:"+requisition),None


def _extract_location(text):
    # Only lines explicitly labeled as location qualify as extracted fields;
    # other text remains available to the contextual eligibility classifier.
    m=re.search(r"(?:location|based in|work location)\s*[:\-]\s*([^.;\n]{2,120})",text,re.I)
    return m.group(1).strip() if m else "Unknown"


def hashlib_path(url):
    import hashlib
    return hashlib.sha256(url.encode()).hexdigest()[:20]


def _discover_links(page):
    found=[]
    for link in page["page"].links:
        url=link.get("url",""); label=link.get("text","")
        if not public_url(url) or not (APPLY_LINK.search(label) or CAREER_LINK.search(label+" "+url)):
            continue
        ats=detect_public_ats(url)
        found.append({"url":url,"priority":100 if ats else 60,"method":"static_html_link"})
    for url in page["structured"]["career_links"]:
        if public_url(url): found.append({"url":url,"priority":65,"method":"career_page_link"})
    for posting in page["structured"]["job_postings"]:
        for field,priority in (("vacancy_url",95),("application_url",100)):
            url=posting.get(field)
            if url and public_url(url): found.append({"url":url,"priority":priority,"method":"jsonld_"+field})
    return found


def verify_url(target, url, *, timeout=8, max_bytes=4*1024*1024, robots_cache=None, browser_fallback=True, request_budget=24):
    """Try current public ATS APIs, static vacancy HTML and bounded Playwright."""
    started=time.monotonic(); cache=robots_cache if robots_cache is not None else {}; requests=0; bytes_read=0; alternatives=[]; discoveries=[]; last_reason="vacancy_not_found"
    if not public_url(url):
        return {"verified":False,"outcome":"blocked","reason":"non_public_or_non_https_url","method":"input_validation","request_count":0,"bytes_read":0,"alternatives":["none: unsafe URL"]}
    request_budget=max(0,int(request_budget))
    detected=detect_public_ats(url)
    if detected and request_budget >= (2 if detected.get("source") == "smartrecruiters" and detected.get("job_id") else 1):
        alternatives.append("public_ats_api:"+detected["source"])
        try:
            api=fetch_public_ats(url,timeout=timeout)
            requests+=int(api.get("request_count", 1)) if api else 0
            if api and api.get("jobs"):
                for posting in api["jobs"]:
                    board_name=posting.get("company") or detected.get("board")
                    identity_score=employer_similarity(target.get("employer"),board_name)
                    if identity_score>=0.72:
                        match,reason=_posting_match(target,posting,url,api.get("method","public_ats_api"),board=detected.get("board"))
                        if match:
                            match["employer_identity_evidence"].append({"kind":"board_owner_alias","value":board_name,"score":round(identity_score,3)})
                            return {"verified":True,"outcome":"verified","method":api["method"],"evidence":match,"request_count":requests,"bytes_read":bytes_read,"elapsed_seconds":time.monotonic()-started,"alternatives":alternatives,"discovered_urls":[]}
                        last_reason=reason or last_reason
                    else:
                        last_reason="employer_identity_unverified"
                # API may provide a current posting but not enough ownership or
                # apply evidence; inspect the public rendered posting page too.
            elif api and api.get("reason"):
                last_reason="api_unavailable_"+str(api["reason"])
        except (HTTPError,URLError,TimeoutError) as exc:
            requests+=1
            code=getattr(exc,"code",None)
            last_reason="http_"+str(code) if code else type(exc).__name__
            if code in (429,500,502,503,504) or isinstance(exc,(URLError,TimeoutError)):
                return {"verified":False,"outcome":"retryable_failure","reason":last_reason,"method":"public_ats_api","retryable":True,"request_count":requests,"bytes_read":bytes_read,"elapsed_seconds":time.monotonic()-started,"alternatives":alternatives}
        except Exception as exc:
            last_reason="ats_"+type(exc).__name__
    robots_new = origin(url) not in cache
    static_cost = 1 + int(robots_new)
    if requests + static_cost <= request_budget:
        alternatives.append("static_html")
    else:
        last_reason="request_budget_exhausted"
    try:
        if "static_html" not in alternatives:
            page={"error":"request_budget_exhausted"}
        else:
            page=_static_page(url,timeout=timeout,max_bytes=max_bytes,robots_cache=cache)
            requests+=page.get("request_count",0); bytes_read+=page.get("bytes_read",0)
        if page.get("error"):
            last_reason=page["error"]
            if last_reason=="robots_denied":
                return {"verified":False,"outcome":"restricted","reason":last_reason,"method":"robots","retryable":False,"request_count":requests,"bytes_read":bytes_read,"elapsed_seconds":time.monotonic()-started,"alternatives":alternatives}
            if last_reason.startswith("http_status_5"):
                return {"verified":False,"outcome":"retryable_failure","reason":last_reason,"method":"static_html","retryable":True,"request_count":requests,"bytes_read":bytes_read,"elapsed_seconds":time.monotonic()-started,"alternatives":alternatives}
        else:
            result,reason=_static_match(target,page,detected)
            discoveries=_discover_links(page)
            if result:
                return {"verified":True,"outcome":"verified","method":result["method"],"evidence":result,"request_count":requests,"bytes_read":bytes_read,"elapsed_seconds":time.monotonic()-started,"alternatives":alternatives,"discovered_urls":discoveries}
            last_reason=reason or last_reason
    except (HTTPError,URLError,TimeoutError) as exc:
        requests+=1; code=getattr(exc,"code",None); last_reason="http_"+str(code) if code else type(exc).__name__
        if code in (429,500,502,503,504) or isinstance(exc,(URLError,TimeoutError)):
            return {"verified":False,"outcome":"retryable_failure","reason":last_reason,"method":"static_html","retryable":True,"request_count":requests,"bytes_read":bytes_read,"elapsed_seconds":time.monotonic()-started,"alternatives":alternatives}
    except Exception as exc:
        last_reason="static_"+type(exc).__name__
        page=None
    else:
        page=page if "page" in locals() and not page.get("error") else None
    if discoveries:
        return {"verified":False,"outcome":"navigation_queued","reason":last_reason,"method":"static_html","retryable":False,"request_count":requests,"bytes_read":bytes_read,"elapsed_seconds":time.monotonic()-started,"alternatives":alternatives,"discovered_urls":discoveries}
    if browser_fallback and requests + 1 >= request_budget and last_reason not in ("human_verification_gate","robots_denied","employer_identity_unverified"):
        last_reason="request_budget_exhausted"
    if browser_fallback and requests + 1 < request_budget and last_reason not in ("human_verification_gate","robots_denied","employer_identity_unverified"):
        alternatives.append("playwright_dynamic_render")
        try:
            # The crawler performs its own robots.txt lookup before navigation.
            rendered=explore_sync([url],max_pages=1,max_depth=0,per_host=1,delay=0,timeout_ms=timeout*1000,max_requests=request_budget-requests-1)
            requests+=1+sum(p.get("network_requests",0) for p in rendered.get("pages",[]))
            if rendered.get("checkpoints"):
                last_reason="human_verification_gate"
            for rendered_page in rendered.get("pages",[]):
                html_doc=rendered_page.get("html")
                # The crawler persists visible text/links; synthesize a parseable
                # document only for the currently rendered DOM as fallback.
                visible_text=rendered_page.get("visible_text","")
                visible=_VisiblePage(rendered_page.get("url",url))
                visible.feed(html_doc or "")
                visible.text.extend(visible_text.split())
                if rendered_page.get("title"):
                    visible.headings.append(rendered_page["title"])
                visible.links.extend(rendered_page.get("application_links", []))
                structured={"job_postings":rendered_page.get("job_postings",[]),"career_links":rendered_page.get("career_links",[]),"organizations":rendered_page.get("organizations",[])}
                dyn={"url":rendered_page.get("url",url),"page":visible,"text":visible_text,"structured":structured,"status":rendered_page.get("status",200)}
                result,reason=_static_match(target,dyn,detected)
                discoveries.extend(_discover_links(dyn))
                if result:
                    result["method"]="playwright:visible_html"
                    return {"verified":True,"outcome":"verified","method":"playwright:visible_html","evidence":result,"request_count":requests,"bytes_read":bytes_read,"elapsed_seconds":time.monotonic()-started,"alternatives":alternatives,"discovered_urls":discoveries}
                last_reason=reason or last_reason
        except Exception as exc:
            last_reason="browser_"+type(exc).__name__
    return {"verified":False,"outcome":"unverified","reason":last_reason,"method":"static_html+fallbacks","retryable":last_reason in {"http_429","human_verification_gate"},"request_count":requests,"bytes_read":bytes_read,"elapsed_seconds":time.monotonic()-started,"alternatives":alternatives,"discovered_urls":discoveries}
