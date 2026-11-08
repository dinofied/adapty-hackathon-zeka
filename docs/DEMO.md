# Demo guide

## Install on phones

- **Android:** open https://lumen-referrals.onrender.com/download/lumen.apk, allow installs from the browser, open Lumen. Or open `/app` in Chrome → menu → Install.
- **iPhone:** open https://lumen-referrals.onrender.com/app in **Safari** → Share → Add to Home Screen.
- A friend should open the **invite link in Chrome/Safari first**. On Android, download from that invitation, install, return to the invitation and tap **Open Lumen**. This passes the code directly to the wrapper (or the web fallback). The fresh invitation also survives first launch when the browser and wrapper share cookies. If they do not, enter the displayed invitation code before signup. Web installs retain it in the manifest.
- Use a **new email** for the friend. An already-created account keeps its original attribution, including organic attribution; reinstalling does not make it a new referral or repeat rewards.
- Web opening checks for updated content before opening/focusing Lumen. Android opens immediately to preserve the tap's user activation and checks for updates in the background. Android package upgrades cannot be silent and are separate from web updates.
- Before merge, a human must test **Open Lumen** on an Android phone both with the APK installed and without it, and report the native launch/web fallback results in the PR. Browser API mocks do not verify Android intent handling on a device.

## Script (about 3 minutes)

1. **Dashboard → Program**: "The owner decides what a referral earns. These rules are stored in Adapty, as the remote config of a settings paywall."
2. **Phone 1**: sign up → *Refer a friend* → choose a messaging app and send (if native sharing is unavailable, paste the copied link into a message).
3. **Phone 2**: open the link → install → return to the invitation → **Open Lumen** → sign up with a new email. Check the pre-filled invitation code. "The invite is applied; Adapty's segment *Referred users* gives them the *Referral welcome* paywall, with copy from its remote config."
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
