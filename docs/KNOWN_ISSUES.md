# Known issues and pitfalls

Read this before building on the project. Ordered roughly by how likely it is to bite.

## Working as a team

- **Every merge to `main` redeploys and empties the demo index** (dashboard users list, invite clicks, activity). Adapty data survives; users reappear when they sign in again. Work on a separate branch for every feature or fix (team decision), batch merges through pull requests, and **freeze `main` before the demo**.
- **One big file each.** `server.py`, `static/app.html` and `static/dashboard.html` hold most of the logic, so parallel edits conflict. Agree who owns which file/feature, keep PRs small, rebase often.
- **CI only tests mock mode.** Adapty-specific failures (wrong field types, locked paywalls, unregistered attributes) only show up live. After a deploy, check `/healthz`, the dashboard's "Adapty API calls" panel, or ask Teodor for Render log lines starting with `Adapty ... ->`.
- **Pushing workflow files** (`.github/workflows/*`) fails over HTTPS with the default `gh` token ("without `workflow` scope"). Use an SSH remote or `gh auth refresh -s workflow`.

## Shared Adapty sandbox app

- **Everyone writes to the same Adapty app,** live server and local live servers alike. Test users from different people mix in the same app; use recognisable emails (`yourname.test1@example.com`).
- **A local server in live mode doesn't get Adapty's webhook** (it goes to the Render URL). Rewards for local purchases arrive through the 25-second fallback. Render also receives the same webhook and may reward first; `referral_reward_paid` makes sure it happens once.
- **Local and live dashboards show different user lists:** each server has its own index; Adapty is the only shared truth.
- **Demo purchases are real Adapty revenue** in the team's company (free-plan tracked revenue). Keep them few; the smoke test against the live site makes one.
- **Rate limits / API etiquette:** the dashboard does one `GET profile` per known user per refresh (cached 15 s). Don't lower that or add per-request fan-out; don't loop scripts against the live site.

## Adapty behaviour that already caused bugs

- **Numbers must be floats** in custom attributes (`3.0`); one int in a batch fails the whole profile write. `_attrs()` in `adapty_client.py` handles it; go through it.
- **`installation_meta.device_id` must be a UUID.**
- **New attributes need registering** in the dashboard (Create custom attribute) to show in the segment editor. String attributes need a fixed value list; free-text values can't be filtered. Key and type are permanent. Max 30 attributes per profile, 50 characters per value, key ≤ 30.
- **Adding a tier name** in the program also needs that value added to the registered `referral_tier` attribute, or segments can't select it.
- **Live paywalls are locked** ("not in a draft state"). Make a new paywall and swap it on the placement.
- **Editing placement audiences can stop the Serbia A/B test**: always write its entry back unchanged.
- **Renaming things breaks lookups:** the server finds the program by the paywall title "Referral program settings" and writes default copy by paywall title (`DEFAULT_REMOTE_CONFIGS`); the placement is looked up as `main_paywall`.
- **The program JSON in Adapty must validate.** If it doesn't, the server keeps using the last good program and logs a warning (it won't overwrite your edit); fix the JSON in Adapty or save from the dashboard.
- **Profiles list and charts lag;** search by email/customer user ID instead. **Event feed** is locked (no real store connection).

## Identity and sessions

- **Don't change how customer user IDs or referral codes are derived** (`ids_for` in `server.py`): every existing profile and shared invite link would be orphaned.
- **App sessions and the webhook password derive from `ADAPTY_SECRET_KEY`.** Rotating the key logs every phone out and changes the webhook Authorization value (update it in Adapty → Integrations → Webhook; the new value is printed at startup).
- **Same email = same account.** Case, `+tags` and Gmail dots are folded, so `ana+test@` signs into `ana@`.

## Hosting

- **Render free plan sleeps** after 15 minutes idle (first request ~50 s) and wipes the index. Teodor's PC pings `/healthz` every 10 minutes while it's on. The GitHub Actions schedule asks for the same but has run hours apart, so it's only a weak backup; if the PC is off, expect cold starts. Open the site a minute before the demo.
- **Env vars, logs and the deploy hook are in Teodor's Render account.** Changes there go through him.
- **The service worker caches the app shell.** It is network-first; its served version fingerprints the app HTML, launch helper and worker. Invitation launch checks for updates, and controlled app pages reload on activation. Offline/update failures still open the app; web updates are not Android package upgrades.
- **`localhost:8080` after the local server stops** still shows the cached app shell, and every action fails with "Failed to fetch". Start the server or use the live URL.

## Hackathon rules (still apply)

Only demo/synthetic data, no secrets in the public repo, no load testing or scraping of Adapty, and anything pre-existing must be disclosed in the README (none so far: all code was written 7–8 Nov).
