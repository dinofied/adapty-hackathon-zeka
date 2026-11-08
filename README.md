# adapty-hackathon-zeka: referral programs on Adapty

A user taps **Refer a friend**, the phone's share sheet sends a link, and the friend installs through it. Both users get referral data on their **Adapty profiles**, the **app owner decides the rewards**, and Adapty segments on that data so referred users, top referrers and tiers can each get their own paywall.

Adapty has no referral feature, but its docs describe the grant-access endpoint as handy "if you have bonuses for referrals". This project adds the missing layer and keeps **Adapty as the system of record**: this service only holds a throwaway index of who used the demo, because Adapty's API can't list profiles.

## For the team

- **Agents (Codex, Claude Code):** start with [`AGENTS.md`](AGENTS.md) (Claude Code reads it through [`CLAUDE.md`](CLAUDE.md)).
- **Docs:** [architecture](docs/ARCHITECTURE.md) · [Adapty setup and IDs](docs/ADAPTY_SETUP.md) · [environment, secrets, deploys](docs/OPERATIONS.md) · [demo guide](docs/DEMO.md) · [known issues](docs/KNOWN_ISSUES.md) · [agent tools & MCP](docs/AGENT_TOOLS.md).
- **Deploys:** merge to `main`; GitHub Actions tests and redeploys Render. No Render login needed.
- **Check everything works:** `python3 server.py` then `python3 scripts/smoke_test.py`.

## How it uses Adapty

| Step | Adapty feature |
|---|---|
| Who can refer | Only users with an **active store subscription** in Adapty (granted reward days don't count), so free accounts can't farm rewards. The owner can switch this to everyone |
| A user signs up | **Profile** created with the phone's real platform (iOS / Android), device and OS. The customer user ID is derived from the email, so duplicates, sign-ins and self-referral checks are Adapty lookups |
| Referral data | **Custom attributes**, registered in the dashboard so they show in the segment editor (see below) |
| Friend installs through the link | In production: an **Adapty Attribution deferred deep link** carrying the code as `deferred_data_sub1`, returned by the SDK on first launch. In the demo: the code passes through the link, a cookie or a recent click from the same network |
| Rewards | **Access level grants**, stacking on the current end date. An inviter who already pays gets `referral_credit_days` instead, so their store subscription stays the one Adapty shows |
| Purchases | **Transactions** (App Store or Google Play, per phone), linked to the paywall variation; they show in the profile, analytics and paywall metrics |
| Inviter reward when the friend pays | **Webhook**: Adapty posts `subscription_started` with the friend's attributes; the service rewards the inviter. If no webhook arrives within 25 seconds, the service does it itself; `referral_reward_paid` keeps it to once |
| Which paywall a user sees | **Segments** on the attributes, mapped to paywalls on placement `main_paywall`; paywall copy comes from each paywall's **remote config** |
| The owner's program | **Remote config of the paywall "Referral program settings"**: edited from the dashboard here or directly in Adapty |

### Attributes

| Attribute | On | Registered as |
|---|---|---|
| `referral_status` | everyone | String: organic, referred, referrer |
| `referral_tier` | inviter | String: none, bronze, silver, gold |
| `referrals_made`, `referrals_paid` | inviter | Number |
| `referral_reward_days`, `referral_credit_days` | both / inviter | Number |
| `links_shared`, `link_opens` | inviter | Number |
| `referral_reward_paid` | friend | String: yes |
| `device_platform` | everyone | String: iOS, Android |
| `referral_vip` | inviter (default rule) | String: yes |
| `referral_code`, `referred_by`, `display_name`, `last_referred` | everyone / friend | not registered (free text; Adapty needs a fixed value list for string filters) |

Adapty allows 30 attributes per profile and 50 characters per value; the invite graph itself stays in this service's index.

## What's where

| URL | For | Login |
|---|---|---|
| `/app` | The phone app: installable to the home screen, real share sheet over HTTPS | none (each phone signs in by email) |
| `/r/<CODE>` | Invite link: install instructions per platform, passes the code into the app | none |
| `/download/lumen.apk` | Android app (PWABuilder package of `/app`) | none |
| `/dashboard` | App owner's view, styled as a proposed **Referrals** section of Adapty; every other menu item opens the real Adapty dashboard | `ADMIN_PASSWORD` |
| `/webhooks/adapty` | Adapty's webhook | Adapty's Authorization header |

## Run it

Needs Python 3.9+ and nothing else.

```bash
cp .env.example .env     # fill in the keys
python3 server.py        # http://localhost:8080
```

- **Without `ADAPTY_SECRET_KEY`**: **mock mode**, no network calls, same screens. The fallback if venue Wi-Fi fails.
- **With the key**: **live** against that Adapty app. Use a sandbox app, never production.
- **Webhook**: unless `WEBHOOK_SECRET` is set, the Authorization value is derived from the secret key and printed once at startup ("Adapty webhook setup"). Put it in Adapty → Integrations → Webhook with the URL `<site>/webhooks/adapty`, and turn on **Send user attributes**.

### Adapty setup (done once, in the sandbox app)

1. Products with both App Store and Google Play IDs (`lumen.demo.monthly`, `lumen.demo.annual`), Android platform enabled.
2. Paywalls "Referral welcome (iOS + Android)", "Paywall A (iOS + Android)", and "Referral program settings" (holds the program; not on any placement).
3. Segment **Referred users** (`referral_status` = referred) on placement `main_paywall`, above All users.
4. The custom attributes above, registered under Profiles & Segments → any attribute dropdown → **Create custom attribute**.
5. The webhook integration.

## Deploy on Render

Pushing to `main` deploys: `.github/workflows/deploy.yml` compiles, runs the smoke test in mock mode, then calls Render's deploy hook (repo secret `RENDER_DEPLOY_HOOK_URL`). Setup and secrets: [docs/OPERATIONS.md](docs/OPERATIONS.md). `render.yaml` documents the service. The free plan sleeps after 15 minutes without visits and doesn't keep its disk, so the demo's **index** resets on sleep or deploy; Adapty keeps all profiles, attributes, purchases and the program.

## Demo script (about 3 minutes)

1. **Dashboard → Program:** show the rules, and that they're saved in Adapty (link to the settings paywall).
2. **Phone 1:** sign up, tap **Refer a friend**, send the link.
3. **Phone 2:** open the link, install, open. The invite is applied; Adapty serves the **Referral welcome** paywall with its remote-config copy.
4. **Phone 2:** subscribe. Within seconds Adapty's webhook arrives and Phone 1's reward shows.
5. **Dashboard → Referrers → Open in Adapty ↗:** the real profiles: platform, Google Play / App Store transaction, access levels, referral attributes.
6. **Adapty → Segments → Referred users:** the filter on the registered attribute.

## Honest limits

- **Purchases are demo transactions** sent through Adapty's server-side API; no money moves. They do count as revenue in the sandbox app.
- **The phone app is a web app** (installable, or the APK wrapper), so the Adapty mobile SDK isn't in the loop; the server calls Adapty's APIs instead.
- **Adapty's Profiles list and charts lag** behind new data; search by email or customer user ID is immediate.
- **Event feed** stays locked until the app is connected to a real store app.

## Built during the hackathon

All code in this folder was written on 7 November 2026, during the event. Nothing pre-existing is reused. Research notes made before the event are kept separately and are not part of this project.
