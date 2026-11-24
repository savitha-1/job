# Global Opportunity Intelligence Engine — implementation plan

## Objective
Discover credible, well-paying engineering opportunities open to India-based candidates, including lesser-known employers. Optimize for qualified interviews, not raw applications. Initial candidate profile: early-career backend/full-stack/infrastructure; Go, Python, TypeScript, Node.js, React, Kubernetes; India-based worldwide remote or Bengaluru; compensation preference INR 12–25 LPA or higher, unknown explicitly labeled.

## Sources and reuse candidates
Audit source files, license, pinned revision, dependency health and tests before incorporating code. Candidates: cahumphreys/jobhunter (discovery strategies), Ramcharan747/careerscout (career infrastructure), mattyray/reporadar (technology discovery), Teycir/HiringSignals and LuckyLuciano97/hiring-signals-tracker (temporal signals), crawljax/crawljax (state graphs), microsoft/playwright (browser automation), ChromeDevTools/chrome-devtools-mcp (runtime observation), j4k0xb/webcrack (public JS analysis), commoncrawl (archives), GH Archive (public activity), networkx/networkx (graphs), huggingface/sentence-transformers (semantic retrieval), online-ml/river (incremental ML), moj-analytical-services/splink (entity resolution), ekzhu/datasketch (deduplication), duckdb/duckdb (analytics). No claim that source-level reuse has occurred.

## Stages and release gates
1. Baseline: audit existing engine.py, discover.py, archive_discovery.py, web_evidence.py and tests; execute actual tests; record limitations. Reproduce source adapters with sanitized fixtures and live smoke tests.
2. Discovery: ingest permitted public archive exports, portfolio directories, public GitHub orgs, engineering blogs, public hiring announcements, employer sites; provenance on each edge. Canonical URL and domain resolution; bounded priority frontier, retry/backoff, robots and rate limits. Benchmark novel verified employer yield per 1,000 requests.
3. Browser intelligence: static HTML first, Playwright fallback for public pages; state hashes, SimHash, safe navigation graph, capture public structured JobPosting and documented public data. Never bypass login, CAPTCHA, restrictions, or submit forms during exploration. Test dynamic fixture sites, pagination, distinct vacancies and error states.
4. Identity and time: SQLite WAL for local persistence, DuckDB/Parquet for analytics; companies, jobs, observations, signals, evidence, applications, outcomes, source stats. Stable identity, MinHash candidate pairs, exact evidence checks, temporal snapshots; never mark jobs closed from failed scans.
5. Eligibility: explicit India/worldwide evidence, regional restrictions, employment model, timezone, seniority, role type, compensation. Conflicts -> manual review. Unknown does not mean eligible. Salary normalization records currency, period, exchange date; never fabricate pay. Employer credibility independent of technology fit.
6. Ranking: baseline BM25 and deterministic filters; compare hybrid semantic retrieval, RRF, cross-encoder; graph discovery with PageRank; source allocation via UCB or Thompson sampling; EWMA/change-point signals after adequate history. Add more complex techniques only with measured gains. No fictitious applicant counts or interview probabilities.
7. Application assistant: prepare truthful resume/answers using candidate-approved facts; direct URLs; duplicate prevention; review queue and explicit submission authorization; no CAPTCHA bypass, bulk unsolicited outreach, or applications against site rules. Track actual outcomes and time cost.
8. Validation and operations: structured logs, checkpoint/resume, resource budgets, GitHub Actions offline regression, opt-in rate-limited live smoke tests, reproducible fixtures and time-split evaluation. Local-first on 8 GB Mac, no required paid APIs. Scale only after measuring.

## Proposed acceptance targets (not achieved)
- Process 5,000 candidate records/day in a representative benchmark, not 5,000 submissions.
- No fabricated opportunities in test set; every displayed claim linked to evidence and timestamp.
- >=95% precision on manually labeled India eligibility for confirmed tier; report sample size and uncertainty.
- <1% duplicate rate on labeled set; preserve distinct requisitions.
- >=95% active-link precision on sampled verified listings.
- Compare verified unique eligible opportunities, precision@20, discovery latency, request costs and interview outcomes against baseline.
- CI passes, no uncontrolled browser actions, reproducible command and results before declaring each stage complete.

## Country strategy
Seed UK, Germany, Netherlands, US, Estonia, Ireland, Sweden, Switzerland, Canada, Finland, Denmark, Norway, France, Belgium, Poland, Austria, Portugal, Spain, Singapore, UAE, Australia and New Zealand; expand globally. These are exploration seeds, not proven rankings. Learn from actual eligible yield, compensation and responses. Separate employer headquarters from allowed applicant location.

## Current status
Initial Python primitives and discovery modules were committed earlier. This plan does not certify their correctness, third-party integration, full system readiness, or live benchmark success. No guarantee of complete internet coverage or employment.

## Revised product requirements: resume-to-application automation
User clarification: target 3,000 distinct opportunities/day across distinct employers, never repeated applications to one employer. Treat 3,000 as a capacity/aspiration, not a promise of available eligible vacancies or successful submissions.
- Resume ingestion: local PDF/DOCX/text extraction, structured candidate facts with provenance, immutable original, explicit unknown fields. Never commit personal resume, credentials or mail tokens to this public repository.
- Candidate profile: configurable employment eligibility, experience, salary, timezone, sponsorship, location and preferences; user-approved reusable answers. No fabricated experience, legal declarations or assessment answers.
- Discovery: retain global company-first discovery, public web state graphs, public hiring announcements, ATS feeds where useful; no single-source dependency.
- Application workflow: adapter interface for permitted portals; dry-run default; discover public form schema, classify fields, fill from approved facts, validate required fields, produce review packet and direct URL. Submit only where platform permits automation and user has authorized the submission scope; stop on CAPTCHA, ambiguous questions, assessments, unknown fields, or access restrictions. No account rotation, bypass, or bulk unsolicited mail.
- Employer deduplication: canonical employer identity and stable requisition IDs, persistent idempotency keys, transactional application ledger, reconcile uncertain submission outcomes before retry; default one opportunity per employer per daily batch.
- LLM: optional provider abstraction for extracting requirements and tailoring truthful resumes; schema validation, source citations, untrusted webpage text isolation, no autonomous access to credentials or submission tools. Deterministic rules handle known fields. Local-first and explicit cost budget.
- Email: read authorized mailbox confirmations and recruiter responses, correlate with ledger, flag interview deadlines, deliver daily summaries; no automatic external outreach to scraped email addresses. Use only official invitation/application channels.
- Background operation: explicit configured worker/scheduler, persistent queue, retries with backoff, pause/kill switch, rate budgets per portal, redacted audit logs, failure alerts.
- Benchmark: measure 3,000 unique employer candidates/day and preparation throughput separately from permitted submissions; track verified vacancies, eligibility precision, form accuracy, duplicate submissions (target zero), confirmation rate, and interviews. Never force volume by lowering eligibility standards.
- Release order: offline fixtures and dry runs, sandbox form integration, authorized small live pilot, then scale based on observed accuracy, portal constraints and available eligible vacancies. No unattended live submission until pilot gates pass.
