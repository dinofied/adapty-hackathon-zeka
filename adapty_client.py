"""Client for Adapty's server-side and web APIs, as used by the referral service.

Runs in mock mode when no secret key is configured, so the demo still works offline.
Every call is recorded in CALL_LOG so the dashboard can show what reached Adapty.
"""

import ipaddress
import json
import re
import secrets
import time
import urllib.error
import urllib.request
import uuid
from collections import deque
from datetime import datetime, timedelta, timezone

API = "https://api.adapty.io/api/v2"
CALL_LOG = deque(maxlen=60)
STORES = {"iOS": "app_store", "Android": "play_store"}


def device_from_ua(ua):
    """Platform, OS and device for an Adapty profile, from the phone's browser user agent."""
    ua = ua or ""
    if "Android" in ua:
        inside = ua[ua.find("(") + 1:ua.find(")")] if "(" in ua else ""
        parts = [x.strip() for x in inside.split(";")]
        ver, model = "", ""
        for i, part in enumerate(parts):
            if part.startswith("Android"):
                ver = part[len("Android"):].strip()
                for cand in parts[i + 1:]:
                    cand = cand.split(" Build/")[0].strip()
                    if cand and cand not in ("wv", "K") and not cand.startswith(("Build", "Mobile")):
                        model = cand
                        break
                break
        return {"platform": "Android", "os": f"Android {ver}".strip(), "device": model or "Android phone"}
    m = re.search(r"(?:iPhone|CPU) OS (\d+)[_.]?(\d+)?", ua)
    device = "iPad" if "iPad" in ua else "iPhone"
    return {"platform": "iOS", "os": f"iOS {m.group(1)}{'.' + m.group(2) if m and m.group(2) else ''}" if m else "iOS",
            "device": device}

# The sandbox app's products (store ids as created in Adapty) and their demo prices.
PRODUCTS = {
    "monthly": {"store_product_id": "lumen.demo.monthly", "base_plan_id": "monthly", "price": 9.99, "days": 30, "label": "Monthly"},
    "annual": {"store_product_id": "lumen.demo.annual", "base_plan_id": "annual", "price": 59.99, "days": 365, "label": "Annual"},
}
BY_STORE_ID = {p["store_product_id"]: k for k, p in PRODUCTS.items()}
BY_STORE_ID.update({"sandbox.monthly": "monthly", "sandbox.annual": "annual"})  # older iOS-only products

# Lumen tokens: Adapty's virtual currency TOKENS keeps every user's balance. Token packs are one-time
# purchases; they are not subscriptions, so they never count in BY_STORE_ID or has_paid_subscription.
TOKEN_CURRENCY = "TOKENS"
TOKEN_PACKS = {
    "tokens_30": {"store_product_id": "lumen.demo.tokens30", "price": 1.99, "tokens": 30, "label": "30 tokens"},
    "tokens_100": {"store_product_id": "lumen.demo.tokens100", "price": 4.99, "tokens": 100, "label": "100 tokens"},
}
TOKEN_PACK_IDS = {p["store_product_id"] for p in TOKEN_PACKS.values()}

# Remote configs the service writes once to the paywalls, so their copy lives in Adapty.
DEFAULT_REMOTE_CONFIGS = {
    "Referral welcome (iOS + Android)": {"title": "A gift from your friend", "subtitle": "You were invited, so you get our best plan first.",
                         "highlight": "annual", "badge": "Friends price", "cta": "Claim my offer"},
    "Paywall A (iOS + Android)": {"title": "Unlock Premium", "subtitle": "Unlimited AI edits, no watermark, every filter.",
                                  "highlight": "monthly", "badge": "Most popular", "cta": "Continue"},
    "Paywall B - annual first": {"title": "Go Premium for a year", "subtitle": "Our best value: every feature, all year.",
                                 "highlight": "annual", "badge": "Best value", "cta": "Continue"},
}


def _now():
    return datetime.now(timezone.utc)


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def device_uuid(user_id):
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"lumen-referrals/{user_id}")).upper()


class AdaptyClient:
    def __init__(self, secret_key, public_key, placement_id, access_level, referred_paywall_title):
        self.secret_key = secret_key or ""
        self.public_key = public_key or ""
        self.placement_id = placement_id
        self.access_level = access_level
        self.referred_paywall_title = referred_paywall_title
        self.mock = not self.secret_key
        self._mock_profiles = {}

    # ---------- internals ----------

    def _log(self, method, path, status, note=""):
        CALL_LOG.appendleft({"at": _now().strftime("%H:%M:%S"), "method": method, "path": path,
                             "status": status, "note": note, "mock": self.mock})

    def _request(self, method, path, body=None, user_id=None, key=None, note="", platform=None):
        headers = {"Authorization": f"Api-Key {key or self.secret_key}", "Content-Type": "application/json",
                   "Accept": "application/json"}
        if user_id:
            headers["adapty-customer-user-id"] = user_id
            headers["adapty-platform"] = platform or "iOS"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(f"{API}{path}", data=data, method=method, headers=headers)
        started = time.time()
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                raw = resp.read().decode() or "{}"
                self._log(method, path, resp.status, (note + " " if note else "") + f"{int((time.time() - started) * 1000)} ms")
                return resp.status, json.loads(raw)
        except urllib.error.HTTPError as e:
            raw = e.read().decode(errors="replace")
            self._log(method, path, e.code, raw[:160])
            print(f"Adapty {method} {path} -> {e.code}: {raw[:400]}", flush=True)
            try:
                return e.code, json.loads(raw)
            except ValueError:
                return e.code, {"raw": raw[:500]}
        except (urllib.error.URLError, TimeoutError) as e:
            self._log(method, path, 0, f"network: {e}")
            print(f"Adapty {method} {path} -> network error: {e}", flush=True)
            return 0, {"error": str(e)}

    @staticmethod
    def _attrs(attrs):
        """Adapty takes string or float values; whole numbers must be sent as floats (3 -> 3.0)."""
        def norm(v):
            if isinstance(v, bool):
                return "yes" if v else "no"
            if isinstance(v, (int, float)):
                return float(v)
            return str(v)
        return [{"key": k, "value": norm(v)} for k, v in attrs.items()]

    @staticmethod
    def _data(d):
        return d.get("data", d) if isinstance(d, dict) else d

    # ---------- profiles ----------

    def create_profile(self, user_id, email, name, attrs, ip=None, device=None):
        """Create a profile with the phone's real platform. Returns (ok, {"profile_id": ...})."""
        device = device or {"platform": "iOS", "os": "iOS", "device": "iPhone"}
        if self.mock:
            pid = str(uuid.uuid4())
            self._mock_profiles[user_id] = {"attrs": dict(attrs), "access": [], "profile_id": pid, "subs": [], "revenue": 0.0}
            self._log("POST", "/server-side-api/profile/", 201, f"mock: {user_id} ({device['platform']})")
            return True, {"profile_id": pid}
        body = {
            "email": email, "first_name": name, "store": STORES[device["platform"]],
            "custom_attributes": self._attrs(attrs),
            "installation_meta": {"device_id": device_uuid(user_id), "device": device["device"], "locale": "en",
                                  "os": device["os"], "platform": device["platform"], "timezone": "Europe/Belgrade",
                                  "app_build": "1", "app_version": "1.0.0"},
        }
        try:
            if ip and ipaddress.ip_address(ip).version == 4 and ipaddress.ip_address(ip).is_global:
                body["ip_v4_address"] = ip
        except ValueError:
            pass
        status, data = self._request("POST", "/server-side-api/profile/", body, user_id=user_id,
                                     platform=device["platform"], note=device["platform"])
        d = self._data(data) if isinstance(data, dict) else {}
        return 200 <= status < 300, {"profile_id": d.get("profile_id") if isinstance(d, dict) else None, "response": data}

    def update_attributes(self, user_id, attrs):
        if self.mock:
            self._mock_profiles.setdefault(user_id, {"attrs": {}, "access": []})["attrs"].update(attrs)
            self._log("PATCH", "/server-side-api/profile/", 200, f"mock: {user_id} {attrs}")
            return True, {"mock": True}
        status, data = self._request("PATCH", "/server-side-api/profile/", {"custom_attributes": self._attrs(attrs)},
                                     user_id=user_id)
        return 200 <= status < 300, data

    def get_profile(self, user_id):
        """(ok, profile) — ok is False and profile is {"missing": True} when Adapty has no such profile."""
        if self.mock:
            self._log("GET", "/server-side-api/profile/", 200, f"mock: {user_id}")
            p = self._mock_profiles.get(user_id)
            if p is None:
                return False, {"missing": True}
            return True, {"custom_attributes": dict(p["attrs"]), "access_levels": list(p["access"]),
                          "profile_id": p.get("profile_id"), "subscriptions": list(p.get("subs", [])),
                          "total_revenue_usd": p.get("revenue", 0.0), "mock": True}
        status, data = self._request("GET", "/server-side-api/profile/", user_id=user_id)
        if 200 <= status < 300:
            d = self._data(data)
            attrs = {a.get("key"): a.get("value") for a in d.get("custom_attributes") or []}
            levels = [{"access_level_id": a.get("access_level_id"), "expires_at": a.get("expires_at"),
                       "store_product_id": a.get("store_product_id"), "store": a.get("store")}
                      for a in d.get("access_levels") or []]
            subs = [{"store_product_id": s.get("store_product_id"), "expires_at": s.get("expires_at"),
                     "store": s.get("store"), "is_active": s.get("is_active")} for s in d.get("subscriptions") or []]
            return True, {"custom_attributes": attrs, "access_levels": levels, "subscriptions": subs,
                          "profile_id": d.get("profile_id"), "total_revenue_usd": d.get("total_revenue_usd") or 0}
        if status == 404:
            return False, {"missing": True}
        return False, data

    def has_paid_subscription(self, profile):
        """True when the profile has an active subscription bought in a store (not a granted reward)."""
        now = _now()
        for s in profile.get("subscriptions") or []:
            if s.get("store_product_id") in BY_STORE_ID and s.get("expires_at"):
                try:
                    if datetime.fromisoformat(str(s["expires_at"]).replace("Z", "+00:00")) > now:
                        return True
                except ValueError:
                    pass
        return False

    # ---------- rewards ----------

    def _current_expiry(self, user_id):
        """End of the user's active reward access level, so new rewards stack on top of it."""
        ok, prof = self.get_profile(user_id)
        latest = None
        if ok:
            for a in prof.get("access_levels") or []:
                if a.get("access_level_id") != self.access_level or not a.get("expires_at"):
                    continue
                try:
                    exp = datetime.fromisoformat(str(a["expires_at"]).replace("Z", "+00:00"))
                except ValueError:
                    continue
                if exp > _now() and (latest is None or exp > latest):
                    latest = exp
        return latest

    def grant_access(self, user_id, days):
        starts = _now()
        expires = (self._current_expiry(user_id) or starts) + timedelta(days=days)
        if self.mock:
            self._mock_profiles.setdefault(user_id, {"attrs": {}, "access": []})["access"] = [
                {"access_level_id": self.access_level, "expires_at": _iso(expires)}]
            self._log("POST", "/server-side-api/purchase/profile/grant/access-level/", 200, f"mock: +{days}d")
            return True, {"expires_at": _iso(expires), "mock": True}
        body = {"access_level_id": self.access_level, "starts_at": _iso(starts), "expires_at": _iso(expires)}
        status, data = self._request("POST", "/server-side-api/purchase/profile/grant/access-level/", body,
                                     user_id=user_id, note=f"+{days}d")
        return 200 <= status < 300, {"expires_at": _iso(expires), "response": data}

    # ---------- purchases ----------

    @staticmethod
    def _tx_id(store, now):
        stamp = f"{int(now.timestamp() * 1000)}{secrets.randbelow(1000):03d}"
        return f"9{stamp}" if store == "app_store" else f"GPA.{stamp[:4]}-{stamp[4:8]}-{stamp[8:12]}-{stamp[12:]}"

    def set_transaction(self, user_id, product_key, variation_id=None, platform="iOS", country="RS"):
        """Record a subscription purchase in Adapty (App Store or Google Play, per the phone). It shows in
        analytics, the event feed and the profile, unlocks the product's access level and fires webhooks."""
        p = PRODUCTS[product_key]
        store = STORES.get(platform, "app_store")
        now = _now()
        tx_id = self._tx_id(store, now)
        body = {
            "purchase_type": "subscription", "store": store, "environment": "Production",
            "store_product_id": p["store_product_id"], "store_transaction_id": tx_id,
            "store_original_transaction_id": tx_id, "is_family_shared": False,
            "price": {"country": country, "currency": "USD", "value": p["price"]},
            "purchased_at": _iso(now), "originally_purchased_at": _iso(now),
            "expires_at": _iso(now + timedelta(days=p["days"])), "renew_status": True,
            "variation_id": variation_id, "offer": None, "refunded_at": None, "cancellation_reason": None,
        }
        if store == "play_store":
            body["base_plan_id"] = p["base_plan_id"]
        if self.mock:
            m = self._mock_profiles.setdefault(user_id, {"attrs": {}, "access": [], "subs": [], "revenue": 0.0})
            m["access"].append({"access_level_id": self.access_level, "expires_at": body["expires_at"]})
            m.setdefault("subs", []).append({"store_product_id": p["store_product_id"], "expires_at": body["expires_at"], "store": store})
            m["revenue"] = m.get("revenue", 0.0) + p["price"]
            self._log("POST", "/server-side-api/purchase/set/transaction/", 200, f"mock: {product_key} ({store})")
            return True, {"mock": True}
        status, data = self._request("POST", "/server-side-api/purchase/set/transaction/", body, user_id=user_id,
                                     platform=platform, note=f"{product_key} ${p['price']} {store}")
        return 200 <= status < 300, data

    def set_token_purchase(self, user_id, pack_key, platform="iOS", country="RS"):
        """Record a token pack as a one-time purchase in Adapty, so its revenue shows in analytics.
        It grants no access level; the server credits the tokens separately (see change_tokens)."""
        p = TOKEN_PACKS[pack_key]
        store = STORES.get(platform, "app_store")
        now = _now()
        tx_id = self._tx_id(store, now)
        body = {
            "purchase_type": "one_time_purchase", "store": store, "environment": "Production",
            "store_product_id": p["store_product_id"], "store_transaction_id": tx_id,
            "store_original_transaction_id": tx_id, "is_family_shared": False,
            "price": {"country": country, "currency": "USD", "value": p["price"]},
            "purchased_at": _iso(now), "variation_id": None, "offer": None, "refunded_at": None,
            "cancellation_reason": None,
        }
        if self.mock:
            m = self._mock_profiles.setdefault(user_id, {"attrs": {}, "access": [], "subs": [], "revenue": 0.0})
            m["revenue"] = m.get("revenue", 0.0) + p["price"]
            self._log("POST", "/server-side-api/purchase/set/transaction/", 200, f"mock: {pack_key} ({store})")
            return True, {"mock": True}
        status, data = self._request("POST", "/server-side-api/purchase/set/transaction/", body, user_id=user_id,
                                     platform=platform, note=f"{pack_key} ${p['price']} {store}")
        return 200 <= status < 300, data

    # ---------- tokens (Adapty virtual currency) ----------

    def token_balance(self, user_id):
        """(ok, tokens the user can spend). A profile that never had tokens has a balance of 0."""
        if self.mock:
            m = self._mock_profiles.get(user_id)
            self._log("GET", "/server-side-api/vc/balances/", 200 if m else 404, f"mock: {user_id}")
            return (True, m.get("tokens", 0)) if m is not None else (False, "profile_not_found")
        status, data = self._request("GET", "/server-side-api/vc/balances/", user_id=user_id)
        if 200 <= status < 300:
            row = next((b for b in self._data(data) or [] if b.get("code") == TOKEN_CURRENCY), {})
            return True, int(row.get("available", row.get("balance", 0)) or 0)
        return False, (data.get("error_code") if isinstance(data, dict) else None) or f"http_{status}"

    def change_tokens(self, user_id, amount, reason, item=None):
        """Credit (amount > 0) or spend (amount < 0) tokens in one atomic Adapty transaction.
        (True, new balance) or (False, error code), e.g. "insufficient_balance", where nothing changed."""
        meta = {"reason": str(reason)[:200]}
        if item:
            meta["item"] = str(item)[:200]
        path = "/server-side-api/vc/transactions/"
        if self.mock:
            m = self._mock_profiles.get(user_id)
            if m is None:
                return False, "profile_not_found"
            if m.get("tokens", 0) + amount < 0:
                self._log("POST", path, 400, f"mock: insufficient_balance for {amount:+d} {TOKEN_CURRENCY}")
                return False, "insufficient_balance"
            m["tokens"] = m.get("tokens", 0) + amount
            self._log("POST", path, 200, f"mock: {amount:+d} {TOKEN_CURRENCY} ({reason})")
            return True, m["tokens"]
        body = {"items": [{"currency_code": TOKEN_CURRENCY, "amount": int(amount)}], "metadata": meta}
        status, data = self._request("POST", path, body, user_id=user_id, note=f"{amount:+d} {TOKEN_CURRENCY} {reason}")
        if 200 <= status < 300:
            row = next((b for b in (data or {}).get("balances") or [] if b.get("code") == TOKEN_CURRENCY), {})
            return True, int(row.get("available", row.get("balance", 0)) or 0)
        return False, (data.get("error_code") if isinstance(data, dict) else None) or f"http_{status}"

    # ---------- paywalls (Adapty decides) ----------

    def get_paywall(self, user_id, platform="iOS"):
        """Ask Adapty which paywall this user gets on the placement, with its remote config."""
        if self.mock or not self.public_key:
            p = self._mock_profiles.get(user_id) or {"attrs": {}}
            name = self.referred_paywall_title if p["attrs"].get("referral_status") == "referred" else "Paywall A (iOS + Android)"
            self._log("POST", "/web-api/paywall/", 200, f"mock: {name}")
            return True, {"paywall_name": name, "variation_id": None, "remote_config": DEFAULT_REMOTE_CONFIGS.get(name, {}),
                          "products": [{"key": k, **v} for k, v in PRODUCTS.items()], "mock": True}
        body = {"store": STORES.get(platform, "app_store"), "locale": "en", "placement_id": self.placement_id,
                "customer_user_id": user_id}
        status, data = self._request("POST", "/web-api/paywall/", body, key=self.public_key)
        if not 200 <= status < 300:
            return False, data
        d = self._data(data)
        rc = d.get("remote_config") or {}
        rc_data = rc.get("data") if isinstance(rc, dict) else None
        if isinstance(rc_data, str):
            try:
                rc_data = json.loads(rc_data)
            except ValueError:
                rc_data = {}
        products = []
        for prod in d.get("products") or []:
            key = BY_STORE_ID.get(prod.get("vendor_product_id"))
            if key:
                products.append({"key": key, **PRODUCTS[key], "title": prod.get("title")})
        return True, {"paywall_name": d.get("paywall_name"), "paywall_id": d.get("paywall_id"),
                      "variation_id": d.get("variation_id"), "ab_test_name": d.get("ab_test_name"),
                      "remote_config": rc_data or {}, "products": products}

    def record_paywall_view(self, user_id, variation_id, platform="iOS"):
        """Log a paywall view so Adapty's paywall and placement metrics count it."""
        if self.mock or not self.public_key or not variation_id:
            self._log("POST", "/web-api/paywall/visit/", 201, "mock")
            return True
        body = {"visited_at": _iso(_now()), "store": STORES.get(platform, "app_store"), "variation_id": variation_id,
                "customer_user_id": user_id}
        status, _ = self._request("POST", "/web-api/paywall/visit/", body, key=self.public_key)
        return 200 <= status < 300

    # ---------- one-time setup: paywall copy lives in Adapty ----------

    def ensure_remote_configs(self):
        """Give each known paywall a remote config if it has none, so its copy is edited in Adapty."""
        if self.mock:
            return []
        status, data = self._request("GET", "/server-side-api/paywalls/?page%5Bsize%5D=100")
        if not 200 <= status < 300:
            return [f"could not list paywalls ({status})"]
        items = data.get("data", []) if isinstance(data, dict) else []
        done = []
        for pw in items:
            title, pid = pw.get("title"), pw.get("paywall_id")
            if title not in DEFAULT_REMOTE_CONFIGS or not pid:
                continue
            s, full = self._request("GET", f"/server-side-api/paywalls/{pid}/")
            full = self._data(full) if isinstance(full, dict) else {}
            if 200 <= s < 300 and full.get("remote_configs"):
                done.append(f"{title}: kept its remote config")
                continue
            body = {"remote_configs": [{"locale": "en", "data": json.dumps(DEFAULT_REMOTE_CONFIGS[title])}]}
            s, _ = self._request("PUT", f"/server-side-api/paywalls/{pid}/", body, note=title)
            done.append(f"{title}: remote config {'set' if 200 <= s < 300 else f'failed ({s})'}")
        return done

    # ---------- the referral program's settings live in Adapty ----------

    def find_paywall_id(self, title):
        if self.mock:
            return "mock-settings"
        status, data = self._request("GET", "/server-side-api/paywalls/?page%5Bsize%5D=100")
        for pw in (data.get("data", []) if isinstance(data, dict) else []):
            if pw.get("title") == title:
                return pw.get("paywall_id")
        return None

    def read_remote_config(self, paywall_id):
        """The paywall's English remote config as a dict ({} if none)."""
        if self.mock:
            return dict(self._mock_profiles.get("__settings__", {}).get("attrs", {}))
        status, data = self._request("GET", f"/server-side-api/paywalls/{paywall_id}/")
        if not 200 <= status < 300:
            raise RuntimeError(f"could not read paywall {paywall_id} ({status})")
        for rc in self._data(data).get("remote_configs") or []:
            if rc.get("locale") == "en":
                raw = rc.get("data")
                return json.loads(raw) if isinstance(raw, str) and raw else (raw or {})
        return {}

    def write_remote_config(self, paywall_id, config, note=""):
        if self.mock:
            self._mock_profiles["__settings__"] = {"attrs": dict(config)}
            self._log("PUT", f"/server-side-api/paywalls/{paywall_id}/", 200, "mock: settings saved")
            return True
        body = {"remote_configs": [{"locale": "en", "data": json.dumps(config)}]}
        status, _ = self._request("PUT", f"/server-side-api/paywalls/{paywall_id}/", body, note=note)
        return 200 <= status < 300
