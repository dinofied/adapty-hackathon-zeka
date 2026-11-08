# Adapty setup (sandbox app)

Everything lives in **"Teodor sandbox (test)"** in the team's Adapty company. Your own Adapty login (invited to the company) sees it; pick it in the dashboard's app switcher. **The Lumen app is the team's seeded demo data: don't modify it.**

## IDs

| Object | Name | ID |
|---|---|---|
| App | Teodor sandbox (test) — iOS `com.teodor.adaptysandbox`, Android `com.lumen.referralsdemo` | `979a0845-3ddd-4cf9-8fcf-739dd33acf4d` |
| Public SDK key (safe to share) | | `public_live_RJYbHI3U.4KTyAa71yQ6chGVoWZqK` |
| Access level | `premium` | `1502cac0-8412-413c-9109-f6b806748338` |
| Product | Monthly (iOS + Android), `lumen.demo.monthly`, $9.99 | `91b9a49f-9343-42ca-92ab-fb8f99974af5` |
| Product | Annual (iOS + Android), `lumen.demo.annual`, $59.99 | `5f59d408-293c-48be-acf1-33a07200446c` |
| Placement | Main paywall, developer ID `main_paywall` | `612ae75a-713f-4b2c-84b4-d447aeba6898` |
| Paywall | Referral welcome (iOS + Android) | `a38ad18e-4965-4c52-8516-7fb376befa4c` |
| Paywall | Paywall A (iOS + Android) | `ee19b2aa-6e49-4120-a2a0-0cb09901a496` |
| Paywall | Paywall B - annual first (in the Serbia A/B test) | `37bd560e-9eeb-4f3c-99b1-6e09dc6d8598` |
| Paywall | **Referral program settings** (holds the program; on no placement) | `da900a87-6add-4065-b92f-cde9ca003a77` |
| Segment | Referred users (`referral_status` = referred) | `f5dc4f2e-1703-433b-b944-efee039d568c` |
| Segment | Serbia users (IP country RS) | `229f3675-be92-458e-ab4f-ed0c776ab917` |
| A/B test | Serbia: monthly-first vs annual-first (**live**) | `656d83c0-15f3-4fc9-8435-8cc8231c7b5f` |

Unused leftovers from earlier iterations (harmless, don't build on them): products `Monthly`/`Annual` (`sandbox.*`, iOS only), paywalls "Referral welcome" and "Paywall A - monthly first" (locked, no longer on the placement).

## Placement `main_paywall` audiences (priority order)

| # | Audience | Shows |
|---|---|---|
| 0 | Serbia users | A/B test "Serbia: monthly-first vs annual-first" (live) |
| 1 | Referred users | Referral welcome (iOS + Android) |
| 2 | All users | Paywall A (iOS + Android) |

Editing audiences: read `adapty placements get --app <app> <placement> --json` first and write the A/B-test entry back **unchanged**, or the test stops with no winner.

## Remote configs

Paywall copy: `{"title", "subtitle", "highlight": "monthly"|"annual", "badge", "cta"}` (locale `en`). Edit in Adapty → Paywalls → paywall → Remote config; the app shows changes on the next paywall open. The server writes defaults only to paywalls that have none.

The program (`Referral program settings` paywall):
```json
{"rules": [
   {"trigger": "install", "recipient": "friend", "action": "grant", "days": 7},
   {"trigger": "first_payment", "recipient": "inviter", "action": "grant", "days": 14},
   {"trigger": "paid_referrals_reached", "n": 3, "recipient": "inviter", "action": "set_attribute", "key": "referral_vip", "value": "yes"}],
 "tiers": [{"name": "bronze", "min_paid": 1}, {"name": "silver", "min_paid": 3}, {"name": "gold", "min_paid": 5}],
 "max_rewarded_referrals": 5}
```
Triggers: `install`, `first_payment`, `paid_referrals_reached`; actions: `grant` (days 1–365) or `set_attribute` (key ≤ 30 chars, value ≤ 50, not a system key).

## Custom attributes (registered)

| Key | Type / values | Written on |
|---|---|---|
| `referral_status` | String: organic, referred, referrer | everyone |
| `referral_tier` | String: none, bronze, silver, gold | inviter |
| `device_platform` | String: iOS, Android | everyone |
| `referral_reward_paid` | String: yes | friend, once the inviter is rewarded |
| `referral_vip` | String: yes | inviter (default rule) |
| `referrals_made`, `referrals_paid` | Number | inviter |
| `referral_reward_days`, `referral_credit_days` | Number | reward recipients / paying inviters |
| `links_shared`, `link_opens` | Number | inviter |

Not registered (free text, so Adapty can't filter on them): `referral_code`, `referred_by`, `display_name`, `last_referred`; plus `referral_rewards` (count used for the cap).

New attribute you want to segment on? Register it: Profiles & Segments → Segments → any attribute dropdown → **Create custom attribute**. Key and type are permanent. If a tier name changes in the program, add the new value to `referral_tier` too.

## Webhook

Integrations → Webhook: production and sandbox URL `https://lumen-referrals.onrender.com/webhooks/adapty`, Authorization header = the value the server prints at startup ("Adapty webhook setup", derived from the secret key), **Send user attributes** on, events include Subscription started, Non-subscription purchase, Trial converted. The server answers the empty verification request without the header.

## Lumen tokens (not set up yet: needs a human)

Mock mode works without any of this. For live mode, in the **sandbox app only**:
Done in the sandbox app on 8 November:

1. Virtual currency `TOKENS` ("Lumen tokens"), with **no linked products**. The server credits tokens after recording a purchase, so linking a product would credit them twice.
2. One-time products `lumen.demo.tokens30` ("Lumen tokens 30", `f044ca64-961d-4b3e-815c-57143f138372`) and `lumen.demo.tokens100` ("Lumen tokens 100", `64ba4dfc-3872-41c2-8066-2db9c2ac61f5`), period lifetime.
3. Access level `token_pack` (`47148943-b105-46b6-9be1-1dcead40defc`) for those products, because Adapty requires one. It unlocks nothing: **never link a pack to `premium`**. The server only reads the `premium` level.

The server API for virtual currencies is enabled for this app. If it returns `403 feature_not_enabled` or `unknown_currency`, the app hides tokens and retries after 5 minutes. Only new signups get welcome tokens; there's no backfill.

## Things that can't be done here

- **Event feed** is locked until the app is linked to a real App Store / Google Play app.
- **Profiles list / Analytics charts** lag behind; search is immediate.
- No API to list profiles or register custom attributes; those are dashboard-only.
