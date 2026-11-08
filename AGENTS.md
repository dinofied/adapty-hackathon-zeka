# AGENTS.md: guide for coding agents (Codex, Claude Code, others)

Team Zeka, Adapty Hackathon Belgrade, 7–8 November 2026. Code freeze **Sunday 8 November, 15:00**; demos at 15:30.

## What this is

**Referral programs on Adapty.** A user taps *Refer a friend*, shares a link, the friend installs through it; both get rewards and referral data on their **Adapty profiles**, the **app owner** configures the rules, and Adapty **segments** on that data pick each user's paywall.

- Live demo: https://lumen-referrals.onrender.com (`/app` phone app, `/dashboard` owner view, password-protected)
- Adapty app: **"Teodor sandbox (test)"** (`979a0845-3ddd-4cf9-8fcf-739dd33acf4d`) in the team's Adapty company. **Never point this at the "Lumen - AI Photo Editor (Hackathon)" app** — that's the seeded demo data the team shares.

## The one design rule

**Adapty is the system of record.** Profiles, referral attributes, rewards (access levels), purchases (transactions), paywall choice (segments + placement), paywall copy (remote config) and even the owner's program (remote config of the paywall "Referral program settings") live in Adapty. This server only keeps a throwaway SQLite index (who used the demo, invite clicks, activity log) because Adapty's API can't list profiles. If you add a feature, put its state in Adapty first.

## Map

| File | What |
|---|---|
| `server.py` | HTTP server, identity, rules engine, webhook handler, admin API (Python stdlib only) |
| `adapty_client.py` | Adapty server-side + web API client, device detection, mock mode |
| `static/app.html` | The phone app (installable web app; real share sheet over HTTPS) |
| `static/app-launch.js` | Update checks and invitation-aware Android/web app opening |
| `static/dashboard.html` | Owner dashboard styled as a proposed *Referrals* section of Adapty |
| `static/invite.html` | Invite landing page `/r/<CODE>` |
| `static/lumen.apk`, `static/assetlinks.json` | Android wrapper of `/app` (PWABuilder) and its Digital Asset Links |
| `scripts/smoke_test.py` | End-to-end check of signup → referral → purchase → reward |
| `scripts/test_compatibility.py` | Isolated HTTP, invitation handoff and Windows UTF-8 regressions (mock Adapty) |
| `scripts/test_app_launch.js` | Dependency-free browser API tests for referral, update and app-opening behavior |
| `tests/test_referrals.py` | Unit tests for identity, program validation, rules engine, caps, once-only payouts, webhook (always mock Adapty) |
| `AI_LOG.md` | How the team used AI: tools, setup, prompts, what went wrong |
| `render.yaml` | Render service definition |
| `.github/workflows/deploy.yml` | CI: compile, unit/HTTP/browser regressions and mock smoke test, then trigger the Render deploy on `main` |
| `.github/workflows/keepalive.yml` | Pings `/healthz` every 10 minutes so Render's free plan doesn't sleep |
| `docs/` | Architecture, Adapty setup, operations & secrets, demo script |

Read `docs/ARCHITECTURE.md` before changing flows, `docs/ADAPTY_SETUP.md` before touching anything in Adapty, and **`docs/KNOWN_ISSUES.md` before starting any feature** (team workflow, shared sandbox, Adapty quirks). Tool and MCP setup for Claude Code and Codex: `docs/AGENT_TOOLS.md`.

## Run, test, deploy

```bash
python3 server.py                                  # mock mode (no keys needed), http://localhost:8080
python3 scripts/smoke_test.py http://localhost:8080 # must print "ALL CHECKS PASSED"
python3 -m unittest discover -s tests              # unit tests; never call Adapty, safe with a live .env
python3 scripts/test_compatibility.py              # isolated mock HTTP and Windows compatibility checks
node --test scripts/test_app_launch.js             # browser API regressions; no npm dependencies
```

- **Live mode locally:** put the sandbox app's secret key in `.env` (see `docs/OPERATIONS.md`). Local servers don't receive Adapty's webhook; inviter rewards then come from the 25-second fallback.
- **Branches:** every feature or fix goes on its own branch off an up-to-date `main` (`feature/...`, `fix/...`), never directly on `main`. Team decision. Push the branch and open a PR; CI runs the checks on the PR. Don't merge unless a human asks.
- **Deploy:** merging to `main` is the deploy. GitHub Actions runs the checks and triggers Render. Nobody needs a Render login. A deploy **wipes the demo's local index** (users list on the dashboard, clicks, activity); everything in Adapty stays.
- **No dependencies.** Keep it standard-library Python and plain HTML/JS: no build step, no `pip install`, no npm.

## Rules for agents

1. **Never commit secrets** (`.env`, Adapty secret key, admin password, deploy hook URL, the Android signing key). The repo is **public**. The public SDK key in `render.yaml` is fine.
2. **Only the sandbox app.** Don't create, change or delete anything in the Lumen app.
3. **Ask a human before irreversible Adapty actions:** stopping or starting A/B tests (can't be undone), deleting segments/paywalls/attributes/profiles, changing the live placement's audiences, changing the webhook integration. Writes that remove or replace an A/B-test audience entry **stop that test**.
4. **Adapty quirks that already bit us** (details in `docs/ADAPTY_SETUP.md`):
   - Number attributes must be sent as floats (`3.0`), or the whole profile write fails.
   - `installation_meta.device_id` must be a UUID.
   - Custom attributes need to be **registered** in the dashboard to appear in the segment editor; string attributes need a fixed value list; keys ≤ 30 chars, values ≤ 50, max 30 per profile. Registered keys/types can't be edited.
   - A paywall that has been live can't be edited ("not in a draft state"): create a new one and swap it on the placement.
   - Adapty's Profiles list and charts lag; search by email/customer user ID is immediate.
5. **Customer user IDs are derived from the email** (`lumen_<8 hex>`), referral codes are `NAME-<same 8 hex>`. Changing either scheme orphans every existing profile — don't.
6. **Purchases are demo transactions** (no real payment), but they count as revenue in the sandbox app. Keep test purchases few.
7. **Hackathon rules:** code must be written during the event (it was, 7–8 Nov); only demo/synthetic data; no load tests or scraping of Adapty's APIs; keep secrets in `.env`.
8. Work on a branch, never on `main` (see Branches above). Before pushing: `python3 -m py_compile server.py adapty_client.py` and the smoke test against a local mock server. Small commits, clear messages.

## Tools for agents

Full setup for Claude Code and Codex in **`docs/AGENT_TOOLS.md`**. In short:
- `npx adapty skills install` — Adapty's skills **and** its MCP server (`https://mcp.adapty.io/mcp`, sign in with your own Adapty account).
- Adapty CLI: `npm install -g adapty@latest && adapty auth login`.
- Playwright MCP (`.mcp.json` declares it for Claude Code; `codex mcp add playwright -- npx -y @playwright/mcp@latest` for Codex) for dashboard-only tasks like registering custom attributes.
- `gh` for Actions runs and logs.
- Adapty docs for agents: add `.md` to any docs URL; indexes at `https://adapty.io/docs/api-llms.txt` and `https://adapty.io/docs/tutorial-llms.txt`.
