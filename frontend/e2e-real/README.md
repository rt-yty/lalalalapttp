# Real recommendation browser acceptance

Run from the repository root:

```sh
npm --prefix frontend run test:e2e:real
```

The suite builds React, starts FastAPI on `127.0.0.1:8792`, registers disposable
accounts through the visible forms, sends messages through the real job API and
checks the resulting database-backed feed. It uses no request interception or
mock recommendation responses. The server owns a temporary SQLite directory and
removes it at shutdown; it never connects to the user's app or database on 8780.
A port conflict fails the run instead of reusing an unknown server.

The gateway uses deterministic `rules` mode and external search is disabled.
These tests prove the HTTP/UI/storage workflow, not live-model quality, real
concert availability or current fares. Those require a separate live run.

Coverage:

- Real signup and startup ideas; recommendation clicks remain editable drafts.
- Search interest and later budget changes reach the homepage.
- Dismissal survives reload, logout/login and stays private to its account.
- Three distinct searchers create real community popularity for a newcomer;
  editorial starter ideas stay under a separate heading.
- Saving the departure city immediately refreshes homepage prompts.
- Concert interests produce research prompts; keyboard use and 320px layouts
  remain usable and pass automated WCAG checks.
- Sending a generated hotel-only family recommendation preserves the destination,
  origin, adults, child ages, budget amount/scope and requested service.

Screenshots go to `frontend/artifacts/recommendations-real/`. Failure traces and
HTML reports use separate `test-results-real/` and `playwright-report-real/`
folders. Each run starts with a fresh database.

The regular `e2e/recommendations.spec.ts` additionally injects outages, malformed
responses and delayed dismissal completion to exercise client error recovery and
race handling. Those are explicitly mocked fault-injection checks.
