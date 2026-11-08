# Architecture

```
 Phone (web app /app, or the Android APK)          Owner (browser /dashboard)
        │  signup, share, purchase, paywall                  │  program, referrers
        ▼                                                    ▼
 ┌──────────────────────── server.py (Render) ─────────────────────────┐
 │ identity  ·  rules engine  ·  webhook handler  ·  admin API        │
 │ SQLite index (throwaway): people, invite clicks, activity log      │
 └──────┬───────────────────────────────┬──────────────────────▲──────┘
        │ server-side API (secret key)  │ web API (public key) │ webhook
        ▼                               ▼                      │
 ┌──────────────────────────────── Adapty ───────────────────────┴─────┐
 │ profiles + custom attributes · access levels · transactions         │
 │ segments → placement main_paywall → paywalls (+ remote config)      │
 │ paywall "Referral program settings" = the owner's program (JSON)    │
 └──────────────────────────────────────────────────────────────────────┘
```

## Identity

- `customer_user_id = "lumen_" + sha256(normalised email)[:8]`, referral code `= NAME5 + "-" + same 8 hex` (e.g. `ANA-3F9A1C2B`). Email normalisation folds case, `+tags` and Gmail dots.
- So: *does this email exist?* = `GET profile`; *who owns code X?* = derive the customer user ID from X and `GET profile`; *self-referral?* = same ID.
- Signing up with an existing email signs the user back in (the profile already exists in Adapty).
- App sessions: a token `customer_user_id.HMAC` (key derived from the Adapty secret key); no server-side session store.

## Flows

**Sign-up (`POST /api/app/signup`)**
1. Detect platform/OS/model from the user agent, refined by Chrome client hints the app sends (`device`).
2. If a `ref` code is given: resolve the inviter in Adapty, block self-referral.
3. Create the Adapty profile (`adapty-platform` header, store `app_store`/`play_store`, `installation_meta` with a UUID device ID, IP for country) with attributes `referral_code`, `display_name`, `device_platform`, `referral_status` (+ `referred_by`).
4. Inviter: `referrals_made += 1`, `last_referred`, `referral_status = referrer` if organic.
5. Run the owner's `install` rules (default: friend gets 7 days premium = access-level grant).

**Invite link (`GET /r/<CODE>`)**: records a click, increments `link_opens` on the inviter, sets a referral cookie, and shows per-platform install steps. The backend retains its cookie and same-IP fallback. The frontend only requests a referral for an explicit `?ref=<CODE>` from the link or the manifest's `start_url`, and verifies the returned code matches. Direct frontend visits ignore legacy cookies, saved codes and shared-IP clicks. In production this would be an Adapty Attribution link with `deferred_data_sub1=<CODE>`.

**Share (`POST /api/app/share`)**: `links_shared += 1`.

**Paywall (`GET /api/app/paywall`)**: web API `POST /web-api/paywall/` for the user's platform → the paywall Adapty picked (segment `referral_status = referred` → "Referral welcome (iOS + Android)", else "Paywall A (iOS + Android)"), its remote config (title, subtitle, badge, highlighted plan, CTA) and products. Logs the view (`/web-api/paywall/visit/`) and remembers `variation_id`.

**Purchase (`POST /api/app/purchase`)**: `POST /server-side-api/purchase/set/transaction/` (App Store or Google Play format per platform, `variation_id` attached). Adapty grants the product's access level and sends `subscription_started` to the webhook.

**Webhook (`POST /webhooks/adapty`)**: on `subscription_started` / `non_subscription_purchase` / `trial_converted`, run `handle_first_payment(friend)`:
1. Skip unless friend's `referral_status = referred` and `referral_reward_paid != yes`; then set `referral_reward_paid = yes` (makes it idempotent).
2. Inviter: `referrals_paid += 1`, `referral_tier` recomputed.
3. Run `first_payment` rules (default: inviter +14 days) and `paid_referrals_reached` rules (default: `referral_vip = yes` at 3).
A 25-second fallback after each purchase calls the same function if the webhook hasn't (e.g. local servers).

**Rewards**: a `grant` rule extends the `premium` access level from its current end date. If the recipient is an inviter who **already has a store subscription**, the days go to `referral_credit_days` instead, so Adapty keeps showing their real subscription. `referral_rewards` counts grants against the program's cap.

**Program (`/api/program`)**: validated JSON `{rules, tiers, max_rewarded_referrals}` stored as the remote config (locale `en`) of the paywall "Referral program settings"; cached 20 s. Editable from the dashboard or directly in Adapty.

## What the dashboard reads

`GET /api/state` (admin): the index's people list, each enriched with a live `GET profile` from Adapty (attributes, revenue, access levels, profile ID for the "Open in Adapty" link). Stats are computed from those Adapty values.

## Mock mode

Without `ADAPTY_SECRET_KEY`, `adapty_client.py` keeps profiles in memory and logs pretend calls; the whole UI works offline. Used by CI and as the venue-Wi-Fi fallback.
