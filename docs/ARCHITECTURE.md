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

**Invite link (`GET /r/<CODE>`)**: records a click, increments `link_opens` on the inviter, and sets a signed, HttpOnly, one-hour pending invitation cookie. A download with an explicit valid `?ref=<CODE>` refreshes that handoff. `/api/app/pending-ref` reads only this authenticated, unexpired intent, never legacy cookies or IP clicks. Signup uses an explicit code first, or the pending intent for older app shells; successful signup/sign-in clears the intent and invalidates the dashboard snapshot. Existing profiles never change attribution or repeat rewards. The editable signup code allows a different-browser fallback; clearing a pre-filled code explicitly declines the pending referral. Failed explicit lookups retain the code so signup cannot silently become organic.

A deleted inviter clears the pending cookie; cookie-derived signup falls back to organic, while an explicit invalid code still fails. Lookup outages do not discard a pending invitation or silently create an organic account. APK downloads continue even if referral validation fails: invalid codes clear the cookie, but lookup exceptions leave it unchanged. Successful signup removes `ref` from the current URL and resets the manifest link; signed-in launches only explain unchanged attribution for a different, non-self invitation.

The legacy `/api/app/ref` cookie/IP fallback remains for compatibility, but direct frontend visits never use it. Explicit links and the install manifest carry `?ref=<CODE>`. Android's **Open Lumen** action targets `com.lumen.referralsdemo` with that URL and a same-code web fallback; after installing the APK, return to the invitation and tap it. Browser/TWA cookie storage may differ, so automatic cross-browser deferred attribution is not promised. Production needs an Adapty Attribution link and native SDK deferred deep-link handling.

**App updates/opening**: Android intent navigation happens immediately in the tap's turn, with the update check running in the background to preserve user activation. Web opening awaits a bounded service-worker check and falls back to ordinary navigation on failure. The worker navigates/focuses an existing app window, or opens one when the browser permits; unchanged windows are only focused. `/sw.js` incorporates a fingerprint of the app HTML, launch helper and worker, so frontend changes trigger updates automatically. Updated controlled app pages reload without losing their URL or session. This refreshes the web content in the APK, not the Android package; package updates require the signing key and OS confirmation.

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
