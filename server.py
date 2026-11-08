"""Referral programs on Adapty: demo server. Python standard library only.

Adapty is the system of record:
- every user is an Adapty profile; their customer user ID is derived from their email,
  so finding a user, spotting duplicates and blocking self-referrals are Adapty lookups
- referral data lives in profile custom attributes (referral_code, referred_by, referrals_made, ...)
- rewards are Adapty access levels; purchases are Adapty transactions
- the inviter's reward is triggered by Adapty's webhook when the friend pays
- the owner's program (rules, tiers, cap) is the remote config of the
  "Referral program settings" paywall in Adapty
This service only keeps a throwaway index of who used the demo (Adapty can't list
profiles over its API), invite-link clicks, and an activity log.

Run:  python3 server.py   (mock mode without ADAPTY_SECRET_KEY)
"""

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote

from adapty_client import CALL_LOG, PRODUCTS as ADAPTY_PRODUCTS, AdaptyClient, device_from_ua

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"
PRODUCTS = {k: v["price"] for k, v in ADAPTY_PRODUCTS.items()}
REFERRED_PAYWALL = "Referral welcome (iOS + Android)"
SETTINGS_PAYWALL = "Referral program settings"
FIRST_PAYMENT_EVENTS = {"subscription_started", "non_subscription_purchase", "trial_converted"}

TRIGGERS = {
    "install": "Friend installs through the link",
    "first_payment": "Friend makes a first payment",
    "paid_referrals_reached": "Inviter reaches N paid referrals",
}
RECIPIENTS = {"friend": "Friend (new user)", "inviter": "Inviter"}
ACTIONS = {"grant": "Grant premium for N days", "set_attribute": "Set an Adapty attribute"}
SYSTEM_KEYS = ("referral_code", "referral_status", "referred_by", "referrals_made", "referrals_paid", "referral_tier",
               "referral_credit_days", "referral_reward_days", "referral_rewards", "referral_reward_paid",
               "links_shared", "link_opens", "last_referred", "display_name", "device_platform")

DEFAULT_PROGRAM = {
    "rules": [
        {"trigger": "install", "recipient": "friend", "action": "grant", "days": 7},
        {"trigger": "first_payment", "recipient": "inviter", "action": "grant", "days": 14},
        {"trigger": "paid_referrals_reached", "n": 3, "recipient": "inviter", "action": "set_attribute",
         "key": "referral_vip", "value": "yes"},
    ],
    "tiers": [{"name": "bronze", "min_paid": 1}, {"name": "silver", "min_paid": 3}, {"name": "gold", "min_paid": 5}],
    "max_rewarded_referrals": 5,
}


def load_env():
    env = {}
    path = ROOT / ".env"
    if path.exists():
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    for k in ("ADAPTY_SECRET_KEY", "ADAPTY_PUBLIC_KEY", "ADAPTY_PLACEMENT_ID", "ADAPTY_ACCESS_LEVEL", "PORT",
              "DATA_DIR", "ADMIN_PASSWORD", "APK_URL", "ASSETLINKS_JSON", "WEBHOOK_SECRET"):
        if os.environ.get(k):
            env[k] = os.environ[k]
    return env


ENV = load_env()
ADAPTY = AdaptyClient(
    secret_key=ENV.get("ADAPTY_SECRET_KEY"),
    public_key=ENV.get("ADAPTY_PUBLIC_KEY"),
    placement_id=ENV.get("ADAPTY_PLACEMENT_ID", "main_paywall"),
    access_level=ENV.get("ADAPTY_ACCESS_LEVEL", "premium"),
    referred_paywall_title=REFERRED_PAYWALL,
)
DATA_DIR = Path(ENV.get("DATA_DIR") or ROOT)
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / ("index-mock.db" if ADAPTY.mock else "index.db")
ADMIN_PASSWORD = ENV.get("ADMIN_PASSWORD", "")
TOKEN_KEY = hashlib.sha256(("lumen-token:" + (ENV.get("ADAPTY_SECRET_KEY") or "mock")).encode()).digest()
# The value Adapty must send in the webhook's Authorization header. Set WEBHOOK_SECRET to choose one;
# otherwise it is derived from the Adapty secret key, so no extra setting is needed.
WEBHOOK_SECRET = ENV.get("WEBHOOK_SECRET") or (
    "" if ADAPTY.mock else "whsec_" + hmac.new(TOKEN_KEY, b"adapty-webhook", hashlib.sha256).hexdigest()[:32])
WEBHOOK_FALLBACK_SECONDS = 25
LOCK = threading.RLock()
USER_LOCKS = {}


class Problem(Exception):
    pass


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def user_lock(cuid):
    with LOCK:
        return USER_LOCKS.setdefault(cuid, threading.Lock())


# ---------- identity: everything derives from the email, so Adapty can be looked up ----------

def email_key(email):
    """Normalise an email so obvious duplicates (case, +tags, Gmail dots) match."""
    email = email.strip().lower()
    local, _, domain = email.partition("@")
    local = local.split("+", 1)[0]
    if domain in ("gmail.com", "googlemail.com"):
        local, domain = local.replace(".", ""), "gmail.com"
    return f"{local}@{domain}"


def ids_for(name, email):
    h = hashlib.sha256(email_key(email).encode()).hexdigest()[:8]
    base = re.sub(r"[^A-Z]", "", name.upper())[:5] or "USER"
    return f"lumen_{h}", f"{base}-{h.upper()}"


def cuid_for_code(code):
    m = re.fullmatch(r"[A-Z]{1,5}-([0-9A-F]{8})", (code or "").strip().upper())
    return f"lumen_{m.group(1).lower()}" if m else None


def make_token(cuid):
    sig = hmac.new(TOKEN_KEY, cuid.encode(), hashlib.sha256).hexdigest()[:24]
    return f"{cuid}.{sig}"


def cuid_from_token(token):
    cuid, _, sig = (token or "").rpartition(".")
    if cuid and hmac.compare_digest(sig, hmac.new(TOKEN_KEY, cuid.encode(), hashlib.sha256).hexdigest()[:24]):
        return cuid
    raise Problem("Not signed in.")


# ---------- local index (throwaway) ----------

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS people (cuid TEXT PRIMARY KEY, name TEXT, email TEXT, code TEXT, referred_by TEXT,
                                           platform TEXT, via TEXT, created_at TEXT, last_variation TEXT);
        CREATE TABLE IF NOT EXISTS clicks (id INTEGER PRIMARY KEY, code TEXT, ip TEXT, at TEXT, claimed INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, at TEXT, kind TEXT, text TEXT);
        """)


def event(kind, text):
    with LOCK, db() as c:
        c.execute("INSERT INTO events (at, kind, text) VALUES (?,?,?)", (now(), kind, text))


def index_person(cuid, **fields):
    with LOCK, db() as c:
        if not c.execute("SELECT 1 FROM people WHERE cuid = ?", (cuid,)).fetchone():
            c.execute("INSERT INTO people (cuid, created_at) VALUES (?, ?)", (cuid, now()))
        for k, v in fields.items():
            if v is not None:
                c.execute(f"UPDATE people SET {k} = ? WHERE cuid = ?", (v, cuid))


def person(cuid):
    with db() as c:
        r = c.execute("SELECT * FROM people WHERE cuid = ?", (cuid,)).fetchone()
    return dict(r) if r else {}


# ---------- Adapty profile helpers ----------

def profile(cuid):
    ok, p = ADAPTY.get_profile(cuid)
    if ok:
        return p
    if p.get("missing"):
        return None
    raise Problem("Adapty didn't answer. Try again in a moment.")


def num(attrs, key):
    try:
        return int(float(attrs.get(key) or 0))
    except (TypeError, ValueError):
        return 0


def premium_until(p):
    latest = None
    for a in (p or {}).get("access_levels") or []:
        if a.get("access_level_id") == ADAPTY.access_level and a.get("expires_at"):
            exp = str(a["expires_at"])
            if exp.replace("Z", "+00:00") > datetime.now(timezone.utc).isoformat() and (latest is None or exp > latest):
                latest = exp
    return latest


# ---------- program: stored in Adapty as a paywall's remote config ----------

_program_cache = {"at": 0.0, "value": None, "paywall": None}


def settings_paywall_id():
    if not _program_cache["paywall"]:
        _program_cache["paywall"] = ADAPTY.find_paywall_id(SETTINGS_PAYWALL)
    return _program_cache["paywall"]


def get_program(fresh=False):
    if not fresh and _program_cache["value"] and time.time() - _program_cache["at"] < 20:
        return _program_cache["value"]
    pid = settings_paywall_id()
    value, empty = None, False
    if pid:
        try:
            raw = ADAPTY.read_remote_config(pid)
            empty = not raw
            value = validate_program(raw) if raw else None
        except (RuntimeError, Problem, ValueError) as e:
            # Unreadable or invalid program in Adapty: keep running on the last good one, and
            # never overwrite what someone saved there (they may be mid-edit).
            print(f"Program in Adapty is invalid or unreadable, using the last good one: {e}", flush=True)
            value = _program_cache["value"]
    if value is None:
        value = _program_cache["value"] or json.loads(json.dumps(DEFAULT_PROGRAM))
        if pid and empty and not ADAPTY.mock:
            ADAPTY.write_remote_config(pid, value, note="default program (remote config was empty)")
    _program_cache.update(at=time.time(), value=value)
    return value


def validate_program(p):
    rules = []
    for r in p.get("rules", []):
        t, who, act = r.get("trigger"), r.get("recipient"), r.get("action")
        if t not in TRIGGERS or who not in RECIPIENTS or act not in ACTIONS:
            raise Problem("Each rule needs a valid trigger, recipient and action.")
        if t == "paid_referrals_reached" and who != "inviter":
            raise Problem("“Inviter reaches N paid referrals” can only reward the inviter.")
        rule = {"trigger": t, "recipient": who, "action": act}
        if t == "paid_referrals_reached":
            rule["n"] = int(r.get("n") or 0)
            if not 1 <= rule["n"] <= 1000:
                raise Problem("N must be between 1 and 1000.")
        if act == "grant":
            rule["days"] = int(r.get("days") or 0)
            if not 1 <= rule["days"] <= 365:
                raise Problem("Premium days must be between 1 and 365.")
        else:
            key, value = str(r.get("key", "")).strip(), str(r.get("value", "")).strip()
            if not re.fullmatch(r"[A-Za-z0-9_.\-]{1,30}", key):
                raise Problem("Attribute keys: 1–30 letters, digits, _ . or -  (Adapty’s limit).")
            if not 1 <= len(value) <= 50:
                raise Problem("Attribute values: 1–50 characters (Adapty’s limit).")
            if key in SYSTEM_KEYS:
                raise Problem(f"“{key}” is set by the referral system itself; choose another key.")
            rule["key"], rule["value"] = key, value
        rules.append(rule)
    tiers = []
    for t in p.get("tiers", []):
        name = str(t.get("name", "")).strip().lower()
        if not re.fullmatch(r"[a-z0-9_-]{1,20}", name):
            raise Problem("Tier names: 1–20 lowercase letters, digits, _ or -.")
        tiers.append({"name": name, "min_paid": max(1, int(t.get("min_paid") or 1))})
    tiers.sort(key=lambda t: t["min_paid"])
    cap = int(p.get("max_rewarded_referrals") or 0)
    if not 1 <= cap <= 1000:
        raise Problem("The reward cap must be between 1 and 1000.")
    return {"rules": rules, "tiers": tiers, "max_rewarded_referrals": cap}


def save_program(p):
    p = validate_program(p)
    pid = settings_paywall_id()
    if not pid:
        raise Problem(f"Paywall “{SETTINGS_PAYWALL}” not found in Adapty.")
    if not ADAPTY.write_remote_config(pid, p, note="program saved"):
        raise Problem("Adapty didn't save the program. Check the server log.")
    _program_cache.update(at=time.time(), value=p)
    event("program", f"Program saved to Adapty: {len(p['rules'])} rules, {len(p['tiers'])} tiers")
    return p


def tier_for(paid, tiers):
    name = "none"
    for t in tiers:
        if paid >= t["min_paid"]:
            name = t["name"]
    return name


# ---------- rules engine: acts on Adapty profiles ----------

def apply_reward(target_cuid, target_name, rule, program, reason, is_inviter):
    """Grant premium (an Adapty access level) or write an attribute. Paying inviters get credit days
    instead of a grant, so their real store subscription stays the one Adapty shows."""
    p = profile(target_cuid) or {}
    attrs = p.get("custom_attributes") or {}
    if rule["action"] == "set_attribute":
        ok, _ = ADAPTY.update_attributes(target_cuid, {rule["key"]: rule["value"]})
        detail = f"{rule['key']} = {rule['value']}"
    else:
        rewards = num(attrs, "referral_rewards")
        if is_inviter and rewards >= program["max_rewarded_referrals"]:
            event("blocked", f"{target_name} hit the cap of {program['max_rewarded_referrals']} rewards")
            return None
        days = rule["days"]
        update = {"referral_reward_days": num(attrs, "referral_reward_days") + days}
        if is_inviter:
            update["referral_rewards"] = rewards + 1
        if is_inviter and ADAPTY.has_paid_subscription(p):
            update["referral_credit_days"] = num(attrs, "referral_credit_days") + days
            ok, _ = ADAPTY.update_attributes(target_cuid, update)
            detail = f"+{days} days credit (already subscribed)"
        else:
            ok, _ = ADAPTY.grant_access(target_cuid, days)
            if ok:
                ADAPTY.update_attributes(target_cuid, update)
            detail = f"+{days} days premium"
    event("reward", f"{target_name}: {detail} ({reason})" + ("" if ok else " — Adapty call failed"))
    return {"to": target_name, "detail": detail, "ok": ok}


def run_rules(trigger, friend, inviter, program, reason):
    done = []
    for rule in program["rules"]:
        if rule["trigger"] != trigger:
            continue
        is_inviter = rule["recipient"] == "inviter"
        who = inviter if is_inviter else friend
        if not who:
            continue
        r = apply_reward(who["cuid"], who["name"], rule, program, reason, is_inviter)
        if r:
            done.append(r)
    return done


def name_of(cuid, attrs=None):
    return (attrs or {}).get("display_name") or person(cuid).get("name") or cuid


def handle_first_payment(friend_cuid, source):
    """Reward the inviter when a referred friend pays for the first time. Idempotent: the friend's
    referral_reward_paid attribute in Adapty records that it already happened."""
    with user_lock(friend_cuid):
        fp = profile(friend_cuid)
        if not fp:
            return []
        fa = fp.get("custom_attributes") or {}
        if fa.get("referral_status") != "referred" or fa.get("referral_reward_paid") == "yes":
            return []
        ADAPTY.update_attributes(friend_cuid, {"referral_reward_paid": "yes"})
    inviter_cuid = cuid_for_code(fa.get("referred_by"))
    program = get_program()
    with user_lock(inviter_cuid or "-"):
        ip_ = profile(inviter_cuid) if inviter_cuid else None
        if not ip_:
            return []
        ia = ip_.get("custom_attributes") or {}
        paid = num(ia, "referrals_paid") + 1
        tier = tier_for(paid, program["tiers"])
        ADAPTY.update_attributes(inviter_cuid, {"referrals_paid": paid, "referral_tier": tier})
    friend = {"cuid": friend_cuid, "name": name_of(friend_cuid, fa)}
    inviter = {"cuid": inviter_cuid, "name": name_of(inviter_cuid, ia)}
    event("payment", f"{friend['name']} paid (via {source}); {inviter['name']} now has {paid} paid referral(s), tier “{tier}”")
    actions = run_rules("first_payment", friend, inviter, program, f"{friend['name']} paid")
    for rule in program["rules"]:
        if rule["trigger"] == "paid_referrals_reached" and rule["n"] == paid:
            actions += run_rules("paid_referrals_reached", friend, inviter, {**program, "rules": [rule]}, f"{paid} paid referrals")
    return actions


# ---------- user actions ----------

def signup(name, email, ref, via, ua, ip, client_device=None):
    name, email = name.strip()[:40], email.strip()
    if not name or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        raise Problem("Enter a name and a valid email.")
    cuid, code = ids_for(name, email)
    device = device_from_ua(ua)
    if client_device and client_device.get("platform") in ("Android", "iOS"):
        device.update({k: str(v)[:40] for k, v in client_device.items() if v and k in ("platform", "os", "device")})
    existing = profile(cuid)
    if existing:  # same email: sign back in, Adapty already knows this person
        attrs = existing.get("custom_attributes") or {}
        index_person(cuid, name=attrs.get("display_name") or name, email=email, code=attrs.get("referral_code") or code,
                     platform=attrs.get("device_platform"))
        return {"token": make_token(cuid), "existing": True, "actions": [], "referred_by": None}
    inviter_cuid = cuid_for_code(ref) if ref else None
    inviter = None
    if ref:
        if inviter_cuid == cuid:
            raise Problem("You can't use your own referral code.")
        ip_ = profile(inviter_cuid) if inviter_cuid else None
        if not ip_:
            raise Problem(f"Referral code {ref.upper()} doesn't exist.")
        inviter = {"cuid": inviter_cuid, "name": name_of(inviter_cuid, ip_.get("custom_attributes")),
                   "attrs": ip_.get("custom_attributes") or {}}
    attrs = {"referral_code": code, "display_name": name, "device_platform": device["platform"],
             "referral_status": "referred" if inviter else "organic"}
    if inviter:
        attrs["referred_by"] = ref.upper()
    ok, created = ADAPTY.create_profile(cuid, email, name, attrs, ip, device)
    if not ok:
        raise Problem("Adapty didn't accept the profile. Check the server log.")
    index_person(cuid, name=name, email=email, code=code, referred_by=inviter["cuid"] if inviter else None,
                 platform=device["platform"], via=via)
    event("install", f"{name} installed on {device['platform']} ({device['device']})"
          + (f" through {inviter['name']}’s link" + (f" via {via}" if via else "") if inviter else ", organic"))
    actions = []
    if inviter:
        program = get_program()
        with user_lock(inviter["cuid"]):
            ia = (profile(inviter["cuid"]) or {}).get("custom_attributes") or {}
            update = {"referrals_made": num(ia, "referrals_made") + 1, "last_referred": name[:50]}
            if ia.get("referral_status") == "organic":
                update["referral_status"] = "referrer"
            ADAPTY.update_attributes(inviter["cuid"], update)
        actions = run_rules("install", {"cuid": cuid, "name": name}, inviter, program, f"{name} joined via {inviter['name']}")
    return {"token": make_token(cuid), "existing": False, "actions": actions,
            "referred_by": inviter["name"] if inviter else None}


def purchase(cuid, product):
    if product not in PRODUCTS:
        raise Problem("Unknown product.")
    p = profile(cuid)
    if not p:
        raise Problem("No such user in Adapty.")
    attrs = p.get("custom_attributes") or {}
    platform = attrs.get("device_platform") or person(cuid).get("platform") or "iOS"
    ok, _ = ADAPTY.set_transaction(cuid, product, person(cuid).get("last_variation"), platform)
    if not ok:
        raise Problem("Adapty didn't accept the purchase. Check the server log.")
    event("purchase", f"{name_of(cuid, attrs)} subscribed: {product} (${PRODUCTS[product]:.2f}) on "
          f"{'Google Play' if platform == 'Android' else 'App Store'}, recorded in Adapty")
    if WEBHOOK_SECRET:
        # Adapty's webhook normally rewards the inviter within seconds. If it hasn't after a short wait,
        # do it here; the friend's referral_reward_paid attribute makes sure it happens only once.
        def fallback():
            time.sleep(WEBHOOK_FALLBACK_SECONDS)
            try:
                if handle_first_payment(cuid, "fallback (no webhook yet)"):
                    print(f"Webhook fallback rewarded the inviter of {cuid}", flush=True)
            except Exception as e:
                print(f"Webhook fallback failed for {cuid}: {e}", flush=True)
        threading.Thread(target=fallback, daemon=True).start()
        return {"recorded": True, "reward": "pending", "note": "Adapty's webhook will reward the inviter"}
    return {"recorded": True, "actions": handle_first_payment(cuid, "purchase")}


def share(cuid, channel):
    with user_lock(cuid):
        attrs = (profile(cuid) or {}).get("custom_attributes") or {}
        ADAPTY.update_attributes(cuid, {"links_shared": num(attrs, "links_shared") + 1})
    event("share", f"{name_of(cuid, attrs)} shared their invite ({channel})")
    return {"ok": True}


def link_opened(code, ip):
    cuid = cuid_for_code(code)
    p = profile(cuid) if cuid else None
    if not p:
        return None
    with LOCK, db() as c:
        c.execute("INSERT INTO clicks (code, ip, at) VALUES (?,?,?)", (code.upper(), ip, now()))
    with user_lock(cuid):
        attrs = (profile(cuid) or {}).get("custom_attributes") or {}
        ADAPTY.update_attributes(cuid, {"link_opens": num(attrs, "link_opens") + 1})
    name = name_of(cuid, attrs)
    event("click", f"Someone opened {name}’s invite link")
    return {"name": name, "code": code.upper()}


def claim_ref(ip):
    with db() as c:
        row = c.execute("SELECT code FROM clicks WHERE ip = ? AND claimed = 0 AND at >= datetime('now', '-2 hours')"
                        " ORDER BY id DESC LIMIT 1", (ip,)).fetchone()
    return row["code"] if row else None


def ref_info(code):
    cuid = cuid_for_code(code)
    p = profile(cuid) if cuid else None
    if not p:
        return {"code": None}
    days = next((r["days"] for r in get_program()["rules"]
                 if r["trigger"] == "install" and r["recipient"] == "friend" and r["action"] == "grant"), None)
    return {"code": code.upper(), "inviter": name_of(cuid, p.get("custom_attributes")), "friend_days": days}


def app_me(cuid):
    p = profile(cuid)
    if not p:
        raise Problem("Not signed in.")
    a = p.get("custom_attributes") or {}
    index_person(cuid, name=a.get("display_name"), code=a.get("referral_code"), platform=a.get("device_platform"))
    with db() as c:
        invited = [dict(r) for r in c.execute("SELECT cuid, name, via FROM people WHERE referred_by = ? ORDER BY created_at", (cuid,))]
    program = get_program()
    rules = program["rules"]
    friend_days = next((r["days"] for r in rules if r["trigger"] == "install" and r["recipient"] == "friend" and r["action"] == "grant"), None)
    inviter_days = next((r["days"] for r in rules if r["trigger"] == "first_payment" and r["recipient"] == "inviter" and r["action"] == "grant"), None)
    inviter_cuid = cuid_for_code(a.get("referred_by"))
    return {
        "name": a.get("display_name") or cuid, "code": a.get("referral_code"), "tier": a.get("referral_tier") or "none",
        "paid": ADAPTY.has_paid_subscription(p) or float(p.get("total_revenue_usd") or 0) > 0,
        "invited_by": name_of(inviter_cuid) if inviter_cuid else None,
        "referrals_made": num(a, "referrals_made"), "referrals_paid": num(a, "referrals_paid"),
        "reward_days": num(a, "referral_reward_days"), "credit_days": num(a, "referral_credit_days"),
        "invited": invited, "premium_until": premium_until(p), "attributes": a,
        "offer": {"friend_days": friend_days, "inviter_days": inviter_days},
        "mode": "mock" if ADAPTY.mock else "live", "platform": a.get("device_platform"),
    }


def app_paywall(cuid):
    attrs = (profile(cuid) or {}).get("custom_attributes") or {}
    platform = attrs.get("device_platform") or "iOS"
    ok, pw = ADAPTY.get_paywall(cuid, platform)
    if not ok:
        return {"error": pw}
    if pw.get("variation_id"):
        index_person(cuid, last_variation=pw["variation_id"])
        ADAPTY.record_paywall_view(cuid, pw["variation_id"], platform)
    return pw


# ---------- admin ----------

# One GET profile per known user per refresh: keep this cached so several open dashboards
# don't multiply calls to Adapty (hackathon rule: be nice to the APIs).
STATE_CACHE_SECONDS = 15
_state_cache = {"at": 0.0, "value": None}


def state():
    if _state_cache["value"] and time.time() - _state_cache["at"] < STATE_CACHE_SECONDS:
        return _state_cache["value"]
    with db() as c:
        people = [dict(r) for r in c.execute("SELECT * FROM people ORDER BY created_at")]
        events = [dict(r) for r in c.execute("SELECT * FROM events ORDER BY id DESC LIMIT 40")]
    by_cuid = {p["cuid"]: p for p in people}
    rows = []
    for pe in people:
        p = profile(pe["cuid"]) or {}
        a = p.get("custom_attributes") or {}
        inviter_cuid = cuid_for_code(a.get("referred_by"))
        invitees = [x["name"] for x in people if x.get("referred_by") == pe["cuid"]]
        revenue = float(p.get("total_revenue_usd") or 0)
        rows.append({
            "id": pe["cuid"], "cuid": pe["cuid"], "name": a.get("display_name") or pe.get("name"), "email": pe.get("email"),
            "code": a.get("referral_code"), "platform": a.get("device_platform") or pe.get("platform"),
            "referred_by_name": name_of(inviter_cuid) if inviter_cuid else None, "via": pe.get("via"),
            "invited": num(a, "referrals_made"), "invited_paid": num(a, "referrals_paid"),
            "invited_names": ", ".join(invitees) or a.get("last_referred") or "",
            "tier": a.get("referral_tier") or "none", "reward_days": num(a, "referral_reward_days"),
            "credit_days": num(a, "referral_credit_days"), "shares": num(a, "links_shared"), "opens": num(a, "link_opens"),
            "revenue": revenue, "first_paid_at": "yes" if revenue > 0 else None, "premium_until": premium_until(p),
            "profile_id": p.get("profile_id"), "adapty_synced": bool(p), "attributes": a,
        })

    def group(rs):
        n, paid = len(rs), sum(1 for r in rs if r["revenue"] > 0)
        rev = sum(r["revenue"] for r in rs)
        return {"users": n, "paid": paid, "conversion": round(100 * paid / n, 1) if n else 0,
                "revenue": round(rev, 2), "revenue_per_user": round(rev / n, 2) if n else 0}

    referred = [r for r in rows if r["referred_by_name"]]
    organic = [r for r in rows if not r["referred_by_name"]]
    value = {
        "mode": "mock" if ADAPTY.mock else "live", "placement": ADAPTY.placement_id,
        "program": get_program(), "catalog": {"triggers": TRIGGERS, "recipients": RECIPIENTS, "actions": ACTIONS},
        "products": PRODUCTS, "users": rows, "rewards": [e for e in events if e["kind"] == "reward"],
        "events": events, "calls": list(CALL_LOG), "webhook": bool(WEBHOOK_SECRET),
        "settings_paywall": SETTINGS_PAYWALL,
        "stats": {"referred": group(referred), "organic": group(organic),
                  "shares": sum(r["shares"] for r in rows), "opens": sum(r["opens"] for r in rows),
                  "reward_days": sum(r["reward_days"] for r in rows)},
    }
    _state_cache.update(at=time.time(), value=value)
    return value


def admin_adapty_view(cuid):
    p = profile(cuid) or {"error": "not found in Adapty"}
    attrs = p.get("custom_attributes") or {}
    ok, pw = ADAPTY.get_paywall(cuid, attrs.get("device_platform") or "iOS")
    return {"profile": p, "paywall": pw if ok else {"error": pw}}


def reset_index():
    with LOCK, db() as c:
        c.executescript("DELETE FROM people; DELETE FROM clicks; DELETE FROM events;")
    CALL_LOG.clear()
    _state_cache["value"] = None


# ---------- HTTP ----------

TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".css": "text/css",
         ".png": "image/png", ".svg": "image/svg+xml", ".json": "application/json",
         ".apk": "application/vnd.android.package-archive"}


def manifest(ref):
    start = "/app" + (f"?ref={ref}" if ref else "")
    return {
        "name": "Lumen: AI Photo Editor", "short_name": "Lumen", "id": "/app", "start_url": start, "scope": "/",
        "display": "standalone", "background_color": "#14213d", "theme_color": "#14213d",
        "description": "Demo app for a referral program built on Adapty.",
        "icons": [
            {"src": "/static/icon-192.png", "sizes": "192x192", "type": "image/png"},
            {"src": "/static/icon-512.png", "sizes": "512x512", "type": "image/png"},
            {"src": "/static/icon-maskable-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
        ],
    }


def clean_code(raw):
    return re.sub(r"[^A-Z0-9-]", "", unquote(raw or "").upper())[:20]


class Handler(BaseHTTPRequestHandler):
    server_version = "LumenReferrals/2.0"

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, payload, ctype="application/json", headers=None):
        body = payload if isinstance(payload, bytes) else (
            payload.encode() if isinstance(payload, str) else json.dumps(payload).encode())
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path, cache=False):
        if not path.is_file():
            return self._send(404, {"error": "Not found"})
        self._send(200, path.read_bytes(), TYPES.get(path.suffix, "application/octet-stream"),
                   {"Cache-Control": "public, max-age=3600"} if cache else None)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        if n > 200_000:
            raise Problem("Request too large.")
        try:
            return json.loads(self.rfile.read(n))
        except ValueError:
            raise Problem("Bad JSON.")

    def _ip(self):
        fwd = self.headers.get("X-Forwarded-For", "")
        return (fwd.split(",")[0].strip() if fwd else self.client_address[0])[:64]

    def _cookie(self, name):
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == name:
                return v
        return None

    def _me(self):
        return cuid_from_token(self.headers.get("X-Token", ""))

    def _admin_ok(self):
        if not ADMIN_PASSWORD:
            return True
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Basic "):
            try:
                _, _, pw = base64.b64decode(auth[6:]).decode().partition(":")
                return hmac.compare_digest(pw, ADMIN_PASSWORD)
            except Exception:
                return False
        return False

    def _need_admin(self):
        if self._admin_ok():
            return False
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="Lumen Referrals admin", charset="UTF-8"')
        self.send_header("Content-Length", "0")
        self.end_headers()
        return True

    def _handle(self, fn):
        try:
            self._send(200, fn())
        except Problem as e:
            self._send(400, {"error": str(e)})
        except Exception as e:  # keep the demo alive, show the error
            print(f"Error: {type(e).__name__}: {e}", flush=True)
            self._send(500, {"error": f"{type(e).__name__}: {e}"})

    def do_GET(self):
        path, _, query = self.path.partition("?")
        params = dict(p.partition("=")[::2] for p in query.split("&") if p)
        if path in ("/", "/index.html"):
            return self._send(302, b"", headers={"Location": "/app"})
        if path == "/healthz":
            return self._send(200, {"ok": True, "mode": "mock" if ADAPTY.mock else "live", "webhook": bool(WEBHOOK_SECRET)})
        if path == "/app":
            ref = clean_code(params.get("ref"))
            page = (STATIC / "app.html").read_text(encoding="utf-8")
            if ref:
                page = page.replace('href="/manifest.webmanifest"', f'href="/manifest.webmanifest?ref={ref}"')
            return self._send(200, page, "text/html; charset=utf-8")
        if path == "/sw.js":
            return self._file(STATIC / "sw.js")
        if path == "/manifest.webmanifest":
            return self._send(200, manifest(clean_code(params.get("ref"))), "application/manifest+json")
        if path == "/.well-known/assetlinks.json":
            raw = ENV.get("ASSETLINKS_JSON") or ((STATIC / "assetlinks.json").read_text(encoding="utf-8")
                                                 if (STATIC / "assetlinks.json").exists() else "[]")
            return self._send(200, raw, "application/json")
        if path.startswith("/static/"):
            name = path[len("/static/"):]
            if not re.fullmatch(r"[A-Za-z0-9_.-]+", name) or name.endswith(".html"):
                return self._send(404, {"error": "Not found"})
            return self._file(STATIC / name, cache=True)
        if path == "/download/lumen.apk":
            if ENV.get("APK_URL"):
                return self._send(302, b"", headers={"Location": ENV["APK_URL"]})
            return self._file(STATIC / "lumen.apk")
        m = re.fullmatch(r"/r/([A-Za-z0-9-]{1,20})", path)
        if m:
            code = clean_code(m.group(1))
            try:
                inv = link_opened(code, self._ip())
            except Problem:
                inv = None
            page = (STATIC / "invite.html").read_text(encoding="utf-8")
            page = (page.replace("{{CODE}}", code)
                        .replace("{{NAME}}", re.sub(r"[<>&\"']", "", inv["name"]) if inv else "A friend")
                        .replace("{{VALID}}", "1" if inv else "0")
                        .replace("{{APK}}", "1" if (ENV.get("APK_URL") or (STATIC / "lumen.apk").exists()) else "0"))
            cookie = f"lumen_ref={code}; Max-Age=86400; Path=/; SameSite=Lax; Secure" if inv else ""
            return self._send(200, page, "text/html; charset=utf-8", {"Set-Cookie": cookie} if cookie else None)
        if path == "/api/app/me":
            return self._handle(lambda: app_me(self._me()))
        if path == "/api/app/paywall":
            return self._handle(lambda: app_paywall(self._me()))
        if path == "/api/app/ref":
            def find():
                code = clean_code(params.get("code")) or clean_code(self._cookie("lumen_ref"))
                if code:
                    info = ref_info(code)
                    if info["code"]:
                        return {**info, "how": "link"}
                found = claim_ref(self._ip())
                return {**ref_info(found), "how": "matched"} if found else {"code": None}
            return self._handle(find)
        if path == "/dashboard" or path.startswith("/api/"):
            if self._need_admin():
                return
        if path == "/dashboard":
            return self._file(STATIC / "dashboard.html")
        if path == "/api/state":
            return self._handle(state)
        m = re.fullmatch(r"/api/users/([A-Za-z0-9_]+)/adapty", path)
        if m:
            return self._handle(lambda: admin_adapty_view(m.group(1)))
        self._send(404, {"error": "Not found"})

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path == "/webhooks/adapty":
            return self._webhook()
        if path == "/api/app/signup":
            def go():
                b = self._body()
                ref = clean_code(b.get("ref")) or None
                r = signup(b.get("name", ""), b.get("email", ""), ref, (b.get("via") or ("Link" if ref else None)),
                           self.headers.get("User-Agent", ""), self._ip(), b.get("device"))
                if ref:
                    with LOCK, db() as c:
                        c.execute("UPDATE clicks SET claimed = 1 WHERE code = ? AND ip = ?", (ref, self._ip()))
                return r
            return self._handle(go)
        if path == "/api/app/share":
            return self._handle(lambda: share(self._me(), str(self._body().get("channel", "share"))[:20]))
        if path == "/api/app/purchase":
            return self._handle(lambda: purchase(self._me(), self._body().get("product", "monthly")))
        if self._need_admin():
            return
        routes = {
            "/api/users": lambda b: signup(b.get("name", ""), b.get("email", ""), clean_code(b.get("code")) or None,
                                           b.get("via"), self.headers.get("User-Agent", ""), self._ip(), b.get("device")),
            "/api/program": lambda b: save_program(b),
            "/api/program/default": lambda b: save_program(DEFAULT_PROGRAM),
            "/api/share": lambda b: share(cuid_for_code(b.get("code")) or "", str(b.get("channel", "link"))[:20]),
            "/api/reset": lambda b: reset_index() or {"ok": True},
        }
        if path in routes:
            return self._handle(lambda: routes[path](self._body()))
        m = re.fullmatch(r"/api/users/([A-Za-z0-9_]+)/purchase", path)
        if m:
            return self._handle(lambda: purchase(m.group(1), self._body().get("product", "monthly")))
        self._send(404, {"error": "Not found"})

    def _webhook(self):
        """Adapty's webhook. Always answers 2xx so Adapty keeps delivering; rewards run in the background."""
        try:
            body = self._body()
        except Problem:
            body = {}
        etype, cuid = body.get("event_type"), body.get("customer_user_id")
        if not etype:  # Adapty's verification request is an empty JSON object: just confirm
            event("webhook", "Adapty webhook connected (verification request)")
            return self._send(200, {"ok": True})
        if WEBHOOK_SECRET and not hmac.compare_digest(self.headers.get("Authorization", ""), WEBHOOK_SECRET):
            print(f"Webhook {etype} rejected: wrong Authorization header", flush=True)
            return self._send(401, {"error": "bad authorization"})
        if etype:
            event("webhook", f"Adapty webhook: {etype} for {name_of(cuid) if cuid else 'unknown user'}")
        if etype in FIRST_PAYMENT_EVENTS and cuid:
            threading.Thread(target=self._reward_safely, args=(cuid,), daemon=True).start()
        self._send(200, {"ok": True})

    @staticmethod
    def _reward_safely(cuid):
        try:
            handle_first_payment(cuid, "Adapty webhook")
        except Exception as e:
            print(f"Webhook reward failed for {cuid}: {e}", flush=True)


def setup_adapty():
    """Paywall copy and the referral program both live in Adapty as remote configs."""
    try:
        results = ADAPTY.ensure_remote_configs()
        get_program(fresh=True)
        results.append("program loaded from Adapty" if settings_paywall_id() else f"“{SETTINGS_PAYWALL}” paywall missing")
    except Exception as e:  # never block the demo on setup
        results = [f"setup failed: {e}"]
    print("Adapty setup: " + "; ".join(results), flush=True)
    event("setup", "Adapty: " + "; ".join(results))


def main():
    # Legacy Windows consoles cannot encode every character in API errors or user names.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="backslashreplace")
    init_db()
    threading.Thread(target=setup_adapty, daemon=True).start()
    port = int(ENV.get("PORT", 8080))
    print(f"Lumen Referrals on http://0.0.0.0:{port}  mode={'MOCK' if ADAPTY.mock else 'LIVE'}"
          f"  webhook={'on' if WEBHOOK_SECRET else 'off'}  admin={'password' if ADMIN_PASSWORD else 'open'}", flush=True)
    if WEBHOOK_SECRET and not ENV.get("WEBHOOK_SECRET"):
        print(f"Adapty webhook setup: endpoint <this site>/webhooks/adapty, Authorization header value {WEBHOOK_SECRET}",
              flush=True)
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
