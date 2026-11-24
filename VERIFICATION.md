# Verification record

Date: 2026-11-24 (Asia/Kolkata). Environment: macOS arm64, Python 3.15.2, Playwright 1.65.2, pypdf 7.1.1, Chromium 159.0.8154.6. The implementation verified below is commit `0452e3f` on `codex/implementation-reconciled`, based on upstream `main` at `1069e16ffd4b471010d5f6f13fae4f16e0d7d267`. The prior locally initialized root history is preserved as `codex/local-prototype`; it was not force-pushed or merged as unrelated history.

## Baseline and final regression

Before implementation, `python3 -m unittest discover -v` passed 7 tests and `python3 -m compileall -q .` passed. After adding discovery, extraction, Gmail, workers, form preparation, and E2E coverage:

- `.venv/bin/python -m unittest discover -v`: **22 tests passed** (unit, integration, and Playwright browser fixture E2E).
- `.venv/bin/python -m compileall -q .`: passed.
- Playwright fixture E2E runs a local delayed-JavaScript site, checks graph edges/state fingerprints, stops at CAPTCHA, maps approved fields, uploads a fixture resume only with its separate authorization flag, and verifies no submit action is exposed.
- Local PDF and DOCX fixtures were parsed. Fake Gmail confirmation matching updated one unambiguous ledger record; no Gmail credentials or real mailbox data were used.
- Security checks cover HTTPS-only public URLs (localhost allowed only in fixture mode), robots denial behavior in the implementation, form automation permission, unapproved facts, sensitive/unknown fields, resume upload opt-in, and no submission path.

During development, the first E2E run found fixture-scheme validation and interaction-gating defects, and delayed JavaScript links were initially missed. Those were corrected; the complete suite was run again and passed. An early comparison command also failed on mixed integer/string job IDs in its own sort key; the measurement command was corrected and rerun.

## Bounded live read-only checks

- Public Greenhouse board `stripe`: 770 jobs returned, no source error.
- Public Ashby board `linear`: 30 jobs returned, no source error.
- Combined fresh request: 800 jobs, 2 employer boards, 0 errors, about 0.95 seconds for these API requests. 378 had one or more configured technology keyword matches; eligibility evidence tagged 75 as India/worldwide signals and left 725 unknown. These are deterministic tags, not manually labeled truth.
- Ordinary ATS keyword baseline versus current deterministic ordering on that same sample: keyword-only top 20 had 0 India/worldwide signals and 20 unknown; current ranking's top 20 had 12 signals and 8 unknown. Evaluation labels: **0**. This is a small heuristic ordering comparison, not evidence of eligibility precision or improved hiring outcomes.
- `https://www.python.org/jobs/`: bounded Playwright read-only crawl visited 2 pages, both HTTP 200 and career-path matches; found 0 structured JobPosting entries and no human/access checkpoint.
- Negative board checks returned 404 for Greenhouse `openai` and Lever `netflix`; no job was fabricated from either. Live checks were limited to public API/page reads; no application form was submitted and no resume or candidate profile was sent.

## Throughput

- `.venv/bin/python benchmark_pipeline.py --count 3000`: **3,000** distinct synthetic employer records passed through the actual local extraction (`inspect_html`), employer/job storage, eligibility ranking, and deterministic form-planning functions in **10.4597 seconds** (286.81 records/second; arithmetic extrapolation 24,780,804/day).
- Benchmark input was generated deterministic HTML; network requests: **0**; live browser navigation: **0**; submissions: **0**. This shows local transformation capacity only. It does not establish 3,000 real unique employer records/day through network discovery or live form review.
- Live coverage observed in this run is 2 employer boards and 2 career pages. The exact blocker to measuring the requested live daily target is lack of a representative, verified 3,000-employer source corpus and enough real discovery observations. Do not extrapolate that volume from this sample.

## Limitations and safety boundaries

- Company identity currently keys by exact host; subsidiaries, parent companies, and ATS brands are not probabilistically resolved. Distinct requisitions use exact source/board/job IDs. Temporal job observations are retained locally.
- GH Archive ingestion accepts a bounded local export; it does not download an unbounded global stream. Common Crawl handling is per-domain or local index export. Hiring signals are not yet evaluated on labeled temporal samples.
- Eligibility remains conservative but heuristic. No gold-labeled India-eligibility set exists, so precision/recall and compensation normalization are unmeasured. Unknown is retained explicitly.
- Resume text parsing is local, facts are suggestions marked unapproved, and original files are read without modification. The repository has no resume or OAuth token. PDF extraction depends on pinned `pypdf`.
- Gmail integration requires the caller to provide an authorized short-lived OAuth token with Gmail read-only scope. No connected mailbox was read; OAuth setup/refresh and daily scheduling against a real mailbox remain unverified. The tested mailbox is fake.
- Browser visits obey robots.txt fail-closed and use same-origin resources only. DNS is not pinned and Chromium is not OS-network isolated, so this is not safe as a service for untrusted users. Navigation does not click dynamic controls or paginate by interaction. CAPTCHA, login and access gates stop exploration.
- Form preparation is dry-run by default, fills only explicitly approved facts when automation permission is asserted, and requires a second explicit flag to upload a resume. CAPTCHA/unknown fields remain human checkpoints. There is no submit method; live submissions remain unimplemented and were not attempted.
- No 3,000-employer/day real discovery claim, universal coverage claim, or hiring outcome claim is made.

## Supplemental implementation verification

This section records the continued implementation in commit `ce6249b` after the earlier `0452e3f` snapshot above. Environment and date are unchanged: macOS arm64, Python 3.15.2, Playwright 1.65.2, Chromium 159.0.8154.6.

- `.venv/bin/python -m unittest discover -v`: **31 tests passed**. This includes unit and integration coverage, local PDF/DOCX fixtures, Playwright E2E against a controlled two-page application form, a scripted non-GET side-effect attempt that was blocked, workflow idempotency/concurrent employer deduplication, human approval reuse, pause/stop controls, and interrupted-submission recovery to an uncertain state.
- `.venv/bin/python -m compileall -q .` and `git diff --check`: passed.
- `.venv/bin/python benchmark_pipeline.py --count 3000`: 3,000 unique synthetic employer records, 3,000 structured job posting parses, and 3,000 deterministic form review plans in **11.3813 seconds** (263.59 records/second; arithmetic extrapolation 22,774,299/day). This runs HTML evidence extraction, employer/job storage, ranking/eligibility and application planning. It used deterministic fixture HTML, 0 network requests, and 0 submissions; it is not live discovery throughput.
- A bounded live read-only smoke through the public HTTPS proxy retrieved **770 Greenhouse Stripe** and **30 Ashby Linear** jobs (800 records total, 0 source errors), then crawled `https://www.python.org/jobs/` with a 2-page/1-depth bound (2 HTTP 200 pages, 2 DOM fingerprints, no checkpoint). No application forms, candidate data, or submissions were involved.
- The earlier ordinary-search comparison remains the only comparison available: on the same 800-posting sample, ATS keyword-only top 20 had 0 heuristic India/worldwide signals, versus 12 of 20 for the eligibility-aware ordering. Human-labeled evaluation count remains 0. This is a heuristic ordering comparison, not a claim of accuracy or hiring outcomes.
- Added a loopback-bound HTTPS CONNECT proxy for urllib and Playwright traffic. It accepts only port 443, resolves once, rejects any non-global DNS answer, connects to the vetted IP, and caps tunnel time and bytes. Browser contexts additionally limit origins; crawl and form contexts allow only GET/HEAD, and form preparation checks robots policy for each explicitly permitted origin. These controls reduce DNS-rebinding/SSRF risk; Chromium is still not OS-network isolated, so this remains a local tool rather than a multi-tenant service.
- Added a durable local application-preparation state machine with mode-700 storage directory / mode-600 database, persistent steps and approval questions, reusable explicitly approved answers, one employer opportunity per batch, pause/stop controls, and crash recovery. A crash after the irreversible submit boundary becomes `uncertain_submission` and cannot be retried automatically. There is still no submission adapter or submit action.
- Gmail confirmation correlation remains read-only and requires a caller-provided authorized OAuth token. Fake mailbox tests passed, but no mailbox, OAuth flow, credentials, or real confirmation data were accessed; scheduled daily summaries remain unverified.
- **The requested 3,000 distinct real employers/day through live discovery and preparation is not established.** The live sample represents two ATS employers and one public career site. There is no representative verified 3,000-employer corpus or enough permitted live observations to claim that rate. The synthetic pipeline result must not be represented as live capacity.

## Bounded global discovery continuation (2026-11-24)

- `.venv/bin/python -m unittest discover -v`: **38 tests passed**, including loopback dashboard/CSRF integration, cross-feed exact URL de-duplication, eligibility/title evidence and the existing Playwright fixture E2E suite. `compileall` and `git diff --check` passed.
- `.venv/bin/python live_discovery.py --output reports/latest-discovery.json` reads four independent public feeds and one capped GH Archive hour. Latest snapshot: **500 feed rows**, **117 relevant source observations**, with exact URL deduplication in the final report. Employer totals are strings from sources, not independently verified legal entities.
- One product worker pass completed the four feeds and GH Archive in **13.2 seconds**: 500 feed rows, 117 role-relevant listings, 3,013 events read from a recent GH Archive hour, 2,590 GitHub activity leads, and 8 GitHub organization profiles resolved. A later repeat completed the feeds but GH Archive resolution returned `HTTPError`; the run is recorded as **partial**, not a total failure. GitHub profile resolution is volatile under unauthenticated rate limits: the latest report attempt resolved 0 profiles. Any resolved profiles remain unverified leads. **0 verified employers, 0 official-source vacancies, 0 direct official application URLs, 0 fully verified India-eligible opportunities, 0 prepared workflows, and 0 confirmed submissions.**
- Latest report counts after exact URL deduplication: **111 provider listings**, **80 employer name strings**, **1 India eligibility text signal**, **92 unknown**, and **18 excluded**; one eligibility signal is not a confirmed eligible opportunity. That row is a Jobicy listing for WIN Home Inspection (APAC location; text says “India-based”) without an employer application URL. Compensation remains unknown.
- A separate live Playwright crawl of Elastic, Camunda and Sentry loaded 7 pages, identified 3 career pages and 0 structured vacancy entries. Sentry showed one human-verification gate; the crawler stopped without interacting. A Common Crawl current-index registry request succeeded; two serial domain queries returned HTTP 504, so no archive discoveries were counted.
- The latest live report command took about **17 seconds** for feed requests and bounded GH Archive/profile lookup. The successful worker pass took 13.2 seconds; a subsequent pass was faster because the GH Archive request failed early. These small live samples do not establish a 3,000-employer/day rate. No synthetic benchmark was used to claim live capacity.
- Start the local loopback dashboard with `python dashboard.py --open`. The artifact [reports/latest-discovery.json](reports/latest-discovery.json) links to provider listing pages for investigation. Mailbox work is deferred. The product still has no live submission adapter, and no application was submitted.
