# Local-first job intelligence prototype

This repository explores public career pages and vacancy APIs, preserves evidence locally, and prepares form review packets from candidate-approved facts. It never submits applications. Unknown fields stay for human review; resume upload requires a separate explicit opt-in.

## Setup

Python application logic uses the standard library. Install the pinned optional browser and PDF tools, then Chromium:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-dev.txt
python -m playwright install chromium
```

Run an ATS fetch from public board APIs:

```sh
python discover.py greenhouse:your-board --output opportunities.json
```

Start the local, loopback-only discovery and application-preparation dashboard:

```sh
python dashboard.py --open
```

Run a bounded cross-source global discovery snapshot and write a ranked public
opportunity report (no candidate data is uploaded):

```sh
python live_discovery.py --output reports/latest-discovery.json --csv-output reports/latest-discovery.csv
```

The snapshot reads Arbeitnow (Germany and UK), Remote OK, Jobicy, the current
Hacker News Who-is-Hiring thread, one capped recent GH Archive hour, and up to
12 public GitHub organization profiles (stopping early if rate-limited).
Aggregator entries remain source observations; they are not called official
vacancies or employer application URLs. GitHub profiles remain unverified
company leads. “India eligibility signal” is distinct from a verified,
applicable opportunity; unknown remains unknown.
HN outbound links are only candidates: the durable verifier checks every
first-link lead before secondary links, follows public ATS APIs when available,
then checks permitted static HTML and a bounded Playwright rendering. It records
identity, title, current-vacancy and job-specific application evidence; robots
denials, closed pages and human-verification gates are not bypassed. Pending URLs
are retained in `~/.cache/job-intelligence/verification.sqlite3`. Tune each run
with `--max-verification-urls` and `--max-verification-requests`.

The multi-strategy live run and its JSON/CSV results are recorded in
[VERIFICATION.md](VERIFICATION.md) and [reports/critical-discovery-2026-11-25.json](reports/critical-discovery-2026-11-25.json).

Run bounded read-only dynamic exploration of a public HTTPS career site:

```python
from web_crawler import explore_sync
from discovery_state import DiscoveryStore

store = DiscoveryStore("data/discovery.sqlite")
result = explore_sync(["https://example.org/careers"], store=store,
                      max_pages=20, max_depth=2, per_host=4, delay=1.0)
```

The explorer checks `robots.txt`, limits visits and same-origin resources, stores a prioritized frontier and state graph, and stops at login/CAPTCHA/access gates. Public requests use a loopback HTTPS proxy that pins DNS resolution to public IPs; browser contexts permit only the active origin and GET/HEAD requests. This is still a local tool, not a multi-tenant service.

Application preparation can be attached to a durable review workflow:

```python
from application_workflow import ApplicationWorkflow
from form_assistant import prepare_form

workflow = ApplicationWorkflow("data/applications.sqlite")
workflow_id = workflow.create(
    "https://jobs.example/apply/role-123", "Example Inc", "2026-11-24"
)
packet = await prepare_form(
    "https://jobs.example/apply/role-123", candidate_profile,
    dry_run=True, workflow=workflow, workflow_id=workflow_id,
)
pending_human_review = workflow.approval_queue(workflow_id)
```

The workflow stores review steps and explicitly approved reusable answers locally, enforces one opportunity per employer per batch, and recovers interrupted preparation/submission states. `paused` and `stopped` controls gate browser preparation. There is no submit adapter; no command or workflow state sends an application. Keep candidate profile and resume files outside Git.

Mailbox tracking is deferred in this milestone. No mailbox access occurs from the dashboard or discovery command.

## Verification

```sh
.venv/bin/python -m unittest discover -v
.venv/bin/python -m compileall -q .
```

See [reports/latest-discovery.json](reports/latest-discovery.json) for the latest bounded live output, [RESEARCH.md](RESEARCH.md) for upstream/license notes, and [VERIFICATION.md](VERIFICATION.md) for measured results and limits. No 3,000-employer/day live capacity claim is made.
