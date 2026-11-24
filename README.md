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

The explorer checks `robots.txt`, limits visits and same-origin resources, stores a prioritized frontier and state graph, and stops at login/CAPTCHA/access gates. DNS is not pinned, so use only with trusted public targets and do not expose it as a service.

For a read-only mailbox integration, construct `GmailReadOnlyMailbox` only after obtaining an OAuth access token with Gmail read-only scope. Tokens and message content are not written to disk. The fake mailbox is used in tests.

## Verification

```sh
.venv/bin/python -m unittest discover -v
.venv/bin/python -m compileall -q .
.venv/bin/python benchmark_pipeline.py --count 3000
```

See [RESEARCH.md](RESEARCH.md) for upstream/license audit and [VERIFICATION.md](VERIFICATION.md) for measured results and limits. Synthetic throughput does not establish live daily employer discovery capacity.
