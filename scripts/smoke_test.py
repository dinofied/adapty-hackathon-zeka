#!/usr/bin/env python3
"""End-to-end smoke test: signup -> referral -> paywall -> purchase -> inviter reward.

Usage:  python3 scripts/smoke_test.py [BASE_URL]      (default http://localhost:8080)

Against a mock-mode server it touches nothing outside this machine. Against the live site
it creates two test profiles and one demo purchase in the Adapty sandbox app: use sparingly.
Exits non-zero on the first failed check.
"""

import json
import secrets
import sys
import time
import urllib.error
import urllib.request

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8080").rstrip("/")
ANDROID_UA = "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Mobile Safari/537.36"


def call(method, path, body=None, token=None):
    req = urllib.request.Request(BASE + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json", "User-Agent": ANDROID_UA,
                                          "X-Token": token or ""})
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def check(cond, what, detail=""):
    print(("  ok   " if cond else "  FAIL ") + what + (f"  ({detail})" if detail and not cond else ""))
    if not cond:
        sys.exit(1)


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="backslashreplace")
    run = secrets.token_hex(3)
    print(f"Smoke test against {BASE} (run {run})")
    s, h = call("GET", "/healthz")
    check(s == 200 and h.get("ok"), "server is up", h)
    print(f"  mode={h.get('mode')} webhook={h.get('webhook')}")

    s, inv = call("POST", "/api/app/signup", {"name": "Smoke Inviter", "email": f"smoke.inviter.{run}@example.com",
                                              "device": {"platform": "Android", "os": "Android 15", "device": "Pixel 8"}})
    check(s == 200 and inv.get("token"), "inviter signs up", inv)
    s, me = call("GET", "/api/app/me", token=inv["token"])
    check(s == 200 and me.get("code") and me.get("platform") == "Android", "inviter profile has a code and platform", me)
    code = me["code"]

    s, again = call("POST", "/api/app/signup", {"name": "Smoke Inviter", "email": f"SMOKE.inviter.{run}+x@example.com"})
    check(s == 200 and again.get("existing"), "same email (case, +tag) signs back in", again)

    s, bad = call("POST", "/api/app/signup", {"name": "X", "email": f"x.{run}@example.com", "ref": "NOPE-00000000"})
    check(s == 400, "unknown referral code is rejected", bad)

    s, _ = call("POST", "/api/app/share", {"channel": "smoke"}, token=inv["token"])
    check(s == 200, "share is recorded")

    s, fr = call("POST", "/api/app/signup", {"name": "Smoke Friend", "email": f"smoke.friend.{run}@example.com", "ref": code})
    check(s == 200 and fr.get("referred_by") == "Smoke Inviter", "friend signs up with the code", fr)
    check(any(a.get("to") == "Smoke Friend" and a.get("ok") for a in fr.get("actions", [])) or not fr.get("actions"),
          "friend's install rules ran", fr.get("actions"))

    s, pw = call("GET", "/api/app/paywall", token=fr["token"])
    check(s == 200 and "error" not in pw, "friend gets a paywall from Adapty", pw)
    check("referral" in (pw.get("paywall_name") or "").lower(), "friend sees the referral paywall", pw.get("paywall_name"))

    s, buy = call("POST", "/api/app/purchase", {"product": "annual"}, token=fr["token"])
    check(s == 200 and buy.get("recorded"), "friend's purchase is recorded", buy)

    deadline = time.time() + 45
    while True:
        s, me = call("GET", "/api/app/me", token=inv["token"])
        if me.get("referrals_paid") == 1 or time.time() > deadline:
            break
        time.sleep(2)
    check(me.get("referrals_paid") == 1, "inviter's paid referral is counted", me.get("attributes"))
    check((me.get("reward_days") or 0) + (me.get("credit_days") or 0) > 0, "inviter is rewarded", me)

    s, buy2 = call("POST", "/api/app/purchase", {"product": "monthly"}, token=fr["token"])
    time.sleep(1 if h.get("mode") == "mock" else 3)
    s, me2 = call("GET", "/api/app/me", token=inv["token"])
    check(me2.get("referrals_paid") == 1, "a second purchase doesn't reward again", me2.get("referrals_paid"))

    print("ALL CHECKS PASSED")


if __name__ == "__main__":
    main()
