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
python discover.py greenhouse:stripe ashby:linear --output opportunities.json
```

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

For read-only mailbox confirmation tracking, construct `GmailReadOnlyMailbox` only after obtaining an OAuth access token with Gmail read-only scope. Tokens and message content are not written to disk. The fake mailbox is used in tests; no mailbox was accessed during verification.

## Verification

```sh
.venv/bin/python -m unittest discover -v
.venv/bin/python -m compileall -q .
.venv/bin/python benchmark_pipeline.py --count 3000
```

See [RESEARCH.md](RESEARCH.md) for upstream/license audit and [VERIFICATION.md](VERIFICATION.md) for measured results and limits. Synthetic throughput does not establish live daily employer discovery capacity.
