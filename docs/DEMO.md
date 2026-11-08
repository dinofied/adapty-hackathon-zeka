# Demo guide

## Install on phones

- **Android:** open https://lumen-referrals.onrender.com/download/lumen.apk, allow installs from the browser, open Lumen. Or open `/app` in Chrome → menu → Install.
- **iPhone:** open https://lumen-referrals.onrender.com/app in **Safari** → Share → Add to Home Screen.
- A friend should open the **invite link in Chrome/Safari first**, then use its app link or install the web app; the explicit referral code carries through the link and install manifest. Opening the app directly without that code does not apply a referral.

## Script (about 3 minutes)

1. **Dashboard → Program**: "The owner decides what a referral earns. These rules are stored in Adapty, as the remote config of a settings paywall."
2. **Phone 1**: sign up → *Refer a friend* → choose a messaging app and send (if native sharing is unavailable, paste the copied link into a message).
3. **Phone 2**: open the link → install → open. "The invite is applied; Adapty's segment *Referred users* gives them the *Referral welcome* paywall, with copy from its remote config."
4. **Phone 2**: subscribe (demo purchase, recorded as an App Store / Google Play transaction in Adapty).
5. **Phone 1** (pull to refresh after ~2 s): "Adapty's webhook told us the friend paid; the inviter got 14 days of premium and moved to tier bronze."
6. **Dashboard → Referrers → Open in Adapty ↗**: the real profiles — platform, transaction, access level, referral attributes.
7. **Adapty → Profiles & Segments → Segments → Referred users**: "Every referral attribute is a first-class Adapty attribute, so growth teams segment on it without code."

## Talking points for judges

- **Who is it for:** subscription apps on Adapty that want word-of-mouth growth without building a referral backend.
- **What changes:** referred users can get their own paywall and offer from day one; rewards are access levels, so no store promo codes; every number lives in Adapty.
- **Why Adapty:** profiles, access levels, transactions, segments, remote config, attribution deep links and webhooks are all Adapty features; the product layer is codes, rules, abuse limits and the dashboard.
- **Honest limits:** purchases are demo transactions; the phone app is a web app (no Adapty mobile SDK); Adapty's Profiles list and charts lag; Event feed needs a real store connection.

## If something breaks on stage

- Dashboard empty after a deploy → expected; users sign in again with their email.
- Reward didn't show → wait 25 s (fallback), then pull to refresh.
- Venue Wi-Fi down → run `python3 server.py` on a laptop (mock mode) and demo from `http://localhost:8080`.
