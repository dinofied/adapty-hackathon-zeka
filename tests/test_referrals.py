"""Unit tests for the risky parts of the referral service: identity, program validation,
the rules engine (rewards, caps, tiers, once-only payouts) and the webhook.

Run:  python -m unittest discover -s tests -v

Always runs against the mock Adapty client and a temporary SQLite index, even when .env
holds a live secret key: nothing here reaches Adapty.
"""

import base64
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="lumen-tests-")

import adapty_client  # noqa: E402
import server  # noqa: E402
from adapty_client import AdaptyClient, device_from_ua, device_uuid  # noqa: E402

IOS_UA = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 Mobile/15E148"
ANDROID_UA = "Mozilla/5.0 (Linux; Android 14; Pixel 8 Build/AP1A) AppleWebKit/537.36 Chrome/130.0 Mobile Safari/537.36"


def fresh_state(test):
    """A mock Adapty, an empty index and empty caches for every test."""
    server.ADAPTY = AdaptyClient(secret_key="", public_key="", placement_id="main_paywall",
                                 access_level="premium", referred_paywall_title=server.REFERRED_PAYWALL)
    assert server.ADAPTY.mock, "tests must never run against live Adapty"
    server.WEBHOOK_SECRET = ""
    server.WEBHOOK_FALLBACK_SECONDS = 25
    # server.db() leaves connections to the garbage collector, so Windows may still hold the file
    tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
    test.addCleanup(tmp.cleanup)
    server.DB_PATH = Path(tmp.name) / "index.db"
    server.init_db()
    server._program_cache.update(at=0.0, value=None, paywall=None)
    server._state_cache.update(at=0.0, value=None)
    server.USER_LOCKS.clear()
    adapty_client.CALL_LOG.clear()


def join(name, email, ref=None, ua=IOS_UA):
    return server.signup(name, email, ref, "Link" if ref else None, ua, "203.0.113.7")


def attrs(cuid):
    return server.profile(cuid)["custom_attributes"]


def cuid(email):
    return server.ids_for("x", email)[0]


def code(name, email):
    return server.ids_for(name, email)[1]


def premium_days_left(c):
    until = server.premium_until(server.profile(c))
    if not until:
        return 0
    delta = datetime.fromisoformat(until.replace("Z", "+00:00")) - datetime.now(timezone.utc)
    return round(delta / timedelta(days=1))


# ---------------------------------------------------------------- identity

class IdentityTests(unittest.TestCase):
    def test_email_key_merges_obvious_duplicates(self):
        k = server.email_key
        self.assertEqual(k("Ana.Smith+promo@Gmail.com"), "anasmith@gmail.com")
        self.assertEqual(k("anasmith@googlemail.com"), "anasmith@gmail.com")
        self.assertEqual(k("  ANA@Example.com "), "ana@example.com")
        # dots only collapse on Gmail; elsewhere they are different mailboxes
        self.assertNotEqual(k("a.na@example.com"), k("ana@example.com"))

    def test_ids_are_stable_and_code_maps_back_to_the_user(self):
        c1, code1 = server.ids_for("Ana", "ana@example.com")
        c2, _ = server.ids_for("Someone else", "ANA+x@example.com")
        self.assertEqual(c1, c2, "same person, same Adapty customer user ID")
        self.assertRegex(c1, r"^lumen_[0-9a-f]{8}$")
        self.assertRegex(code1, r"^ANA-[0-9A-F]{8}$")
        self.assertEqual(server.cuid_for_code(code1), c1)
        self.assertEqual(server.cuid_for_code(code1.lower()), c1, "codes are case-insensitive")

    def test_code_prefix_falls_back_for_names_without_latin_letters(self):
        self.assertRegex(server.ids_for("Ђорђе 123", "d@example.com")[1], r"^USER-[0-9A-F]{8}$")
        self.assertRegex(server.ids_for("Alexandra", "a@example.com")[1], r"^ALEXA-")

    def test_malformed_codes_map_to_no_one(self):
        for bad in (None, "", "ANA", "ANA-123", "ANA-ZZZZZZZZ", "TOOLONG-0123ABCD", "ANA-0123ABCD; DROP"):
            self.assertIsNone(server.cuid_for_code(bad), bad)

    def test_tokens_cannot_be_forged(self):
        tok = server.make_token("lumen_0123abcd")
        self.assertEqual(server.cuid_from_token(tok), "lumen_0123abcd")
        for bad in ("", "lumen_0123abcd", "lumen_0123abcd.deadbeef", tok.replace("lumen_0123abcd", "lumen_ffffffff")):
            with self.assertRaises(server.Problem):
                server.cuid_from_token(bad)


# ---------------------------------------------------------------- program validation

class ProgramValidationTests(unittest.TestCase):
    def valid(self, **over):
        p = json.loads(json.dumps(server.DEFAULT_PROGRAM))
        p.update(over)
        return p

    def rejects(self, program, msg):
        with self.assertRaisesRegex(server.Problem, msg):
            server.validate_program(program)

    def test_default_program_is_valid(self):
        self.assertEqual(server.validate_program(self.valid())["max_rewarded_referrals"], 5)

    def test_rejects_unknown_trigger_recipient_or_action(self):
        for bad in ({"trigger": "birthday", "recipient": "friend", "action": "grant", "days": 1},
                    {"trigger": "install", "recipient": "stranger", "action": "grant", "days": 1},
                    {"trigger": "install", "recipient": "friend", "action": "cash", "days": 1}):
            self.rejects(self.valid(rules=[bad]), "valid trigger")

    def test_milestone_rules_only_reward_the_inviter(self):
        self.rejects(self.valid(rules=[{"trigger": "paid_referrals_reached", "n": 3, "recipient": "friend",
                                        "action": "grant", "days": 7}]), "only reward the inviter")

    def test_day_counts_and_cap_are_bounded(self):
        for days in (0, 366, -5):
            self.rejects(self.valid(rules=[{"trigger": "install", "recipient": "friend", "action": "grant",
                                            "days": days}]), "between 1 and 365")
        self.rejects(self.valid(max_rewarded_referrals=0), "cap")
        self.rejects(self.valid(max_rewarded_referrals=1001), "cap")

    def test_attribute_rules_respect_adapty_limits_and_system_keys(self):
        rule = {"trigger": "install", "recipient": "friend", "action": "set_attribute"}
        self.rejects(self.valid(rules=[{**rule, "key": "k" * 31, "value": "v"}]), "1–30")
        self.rejects(self.valid(rules=[{**rule, "key": "bad key", "value": "v"}]), "1–30")
        self.rejects(self.valid(rules=[{**rule, "key": "promo", "value": "v" * 51}]), "1–50")
        self.rejects(self.valid(rules=[{**rule, "key": "referral_status", "value": "vip"}]), "referral system itself")

    def test_tiers_are_normalised_and_sorted(self):
        p = server.validate_program(self.valid(tiers=[{"name": "Gold", "min_paid": 5}, {"name": "bronze", "min_paid": 1}]))
        self.assertEqual([t["name"] for t in p["tiers"]], ["bronze", "gold"])
        self.rejects(self.valid(tiers=[{"name": "gold tier!", "min_paid": 5}]), "Tier names")

    def test_tier_for_picks_the_highest_reached(self):
        tiers = server.DEFAULT_PROGRAM["tiers"]
        self.assertEqual([server.tier_for(n, tiers) for n in (0, 1, 2, 3, 4, 5, 50)],
                         ["none", "bronze", "bronze", "silver", "silver", "gold", "gold"])


class ProgramStorageTests(unittest.TestCase):
    def setUp(self):
        fresh_state(self)

    def test_invalid_program_in_adapty_is_never_overwritten(self):
        good = server.save_program(server.DEFAULT_PROGRAM)
        broken = {"rules": [{"trigger": "nonsense"}], "max_rewarded_referrals": 5}
        server.ADAPTY.write_remote_config("mock-settings", broken)  # someone mid-edit in Adapty
        self.assertEqual(server.get_program(fresh=True), good, "keeps running on the last good program")
        self.assertEqual(server.ADAPTY.read_remote_config("mock-settings"), broken, "their edit is left alone")


# ---------------------------------------------------------------- signup and referral

class SignupTests(unittest.TestCase):
    def setUp(self):
        fresh_state(self)
        join("Andre", "andre@example.com")
        self.andre, self.code = cuid("andre@example.com"), code("Andre", "andre@example.com")

    def test_rejects_bad_input(self):
        for name, email in (("", "a@example.com"), ("Ana", "not-an-email"), ("Ana", "a@b")):
            with self.assertRaises(server.Problem):
                join(name, email)

    def test_unknown_code_is_rejected_and_creates_no_profile(self):
        with self.assertRaisesRegex(server.Problem, "doesn't exist"):
            join("Mila", "mila@example.com", ref="NOPE-00000000")
        self.assertIsNone(server.profile(cuid("mila@example.com")))

    def test_own_code_is_rejected(self):
        own = code("Mila", "mila@example.com")
        with self.assertRaisesRegex(server.Problem, "own referral code"):
            join("Mila", "mila@example.com", ref=own)

    def test_inviter_signing_in_again_with_own_code_counts_nothing(self):
        r = join("Andre", "ANDRE+again@example.com", ref=self.code)
        self.assertTrue(r["existing"])
        self.assertEqual(server.num(attrs(self.andre), "referrals_made"), 0)

    def test_referred_friend_and_inviter_get_their_attributes(self):
        r = join("Mila", "mila@example.com", ref=self.code, ua=ANDROID_UA)
        self.assertEqual(r["referred_by"], "Andre")
        mila = attrs(cuid("mila@example.com"))
        self.assertEqual(mila["referral_status"], "referred")
        self.assertEqual(mila["referred_by"], self.code)
        self.assertEqual(mila["device_platform"], "Android")
        andre = attrs(self.andre)
        self.assertEqual(andre["referral_status"], "referrer")
        self.assertEqual(andre["referrals_made"], 1)
        self.assertEqual(andre["last_referred"], "Mila")
        self.assertEqual(premium_days_left(cuid("mila@example.com")), 7, "default install rule: friend gets 7 days")

    def test_returning_friend_is_not_counted_twice(self):
        join("Mila", "mila@example.com", ref=self.code)
        again = join("Mila", "Mila@Example.com", ref=self.code)
        self.assertTrue(again["existing"])
        self.assertEqual(server.num(attrs(self.andre), "referrals_made"), 1)

    def test_referred_friend_gets_the_referral_paywall(self):
        join("Mila", "mila@example.com", ref=self.code)
        join("Olga", "olga@example.com")
        self.assertEqual(server.app_paywall(cuid("mila@example.com"))["paywall_name"], server.REFERRED_PAYWALL)
        self.assertNotEqual(server.app_paywall(cuid("olga@example.com"))["paywall_name"], server.REFERRED_PAYWALL)


# ---------------------------------------------------------------- payments and rewards

class RewardTests(unittest.TestCase):
    def setUp(self):
        fresh_state(self)
        join("Andre", "andre@example.com")
        self.andre, self.code = cuid("andre@example.com"), code("Andre", "andre@example.com")

    def friend(self, n):
        email = f"friend{n}@example.com"
        join(f"Friend", email, ref=self.code)
        return cuid(email)

    def test_friend_payment_rewards_inviter_once(self):
        f = self.friend(1)
        server.purchase(f, "annual")
        a = attrs(self.andre)
        self.assertEqual(a["referrals_paid"], 1)
        self.assertEqual(a["referral_tier"], "bronze")
        self.assertEqual(premium_days_left(self.andre), 14)
        server.purchase(f, "monthly")  # renewal / second product
        self.assertEqual(attrs(self.andre)["referrals_paid"], 1)
        self.assertEqual(premium_days_left(self.andre), 14)

    def test_organic_payment_rewards_no_one(self):
        join("Olga", "olga@example.com")
        self.assertEqual(server.purchase(cuid("olga@example.com"), "monthly")["actions"], [])
        self.assertEqual(server.num(attrs(self.andre), "referrals_paid"), 0)

    def test_unknown_product_and_unknown_user_are_rejected(self):
        with self.assertRaises(server.Problem):
            server.purchase(self.andre, "lifetime")
        with self.assertRaises(server.Problem):
            server.purchase("lumen_ffffffff", "monthly")

    def test_rewards_stack_on_existing_premium(self):
        for n in (1, 2):
            server.purchase(self.friend(n), "monthly")
        self.assertEqual(premium_days_left(self.andre), 28)

    def test_reward_cap_stops_grants_but_still_counts_referrals(self):
        program = server.save_program({**server.DEFAULT_PROGRAM, "max_rewarded_referrals": 1})
        self.assertEqual(program["max_rewarded_referrals"], 1)
        for n in (1, 2):
            server.purchase(self.friend(n), "monthly")
        a = attrs(self.andre)
        self.assertEqual(a["referrals_paid"], 2)
        self.assertEqual(a["referral_rewards"], 1)
        self.assertEqual(premium_days_left(self.andre), 14)

    def test_paying_inviter_gets_credit_days_not_a_grant(self):
        server.purchase(self.andre, "annual")
        before = server.premium_until(server.profile(self.andre))
        server.purchase(self.friend(1), "monthly")
        a = attrs(self.andre)
        self.assertEqual(a["referral_credit_days"], 14)
        self.assertEqual(server.premium_until(server.profile(self.andre)), before,
                         "their store subscription stays what Adapty shows")

    def test_milestone_rule_fires_exactly_at_n(self):
        for n in (1, 2):
            server.purchase(self.friend(n), "monthly")
        self.assertNotIn("referral_vip", attrs(self.andre))
        server.purchase(self.friend(3), "monthly")
        self.assertEqual(attrs(self.andre)["referral_vip"], "yes")
        self.assertEqual(attrs(self.andre)["referral_tier"], "silver")
        server.purchase(self.friend(4), "monthly")
        with server.db() as c:
            fired = c.execute("SELECT COUNT(*) FROM events WHERE kind = 'reward' AND text LIKE '%referral_vip%'").fetchone()[0]
        self.assertEqual(fired, 1, "the milestone fires once, not on every later payment")

    def test_webhook_and_fallback_racing_pay_out_once(self):
        f = self.friend(1)
        server.ADAPTY.set_transaction(f, "monthly")
        threads = [threading.Thread(target=server.handle_first_payment, args=(f, f"race {i}")) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(attrs(self.andre)["referrals_paid"], 1)
        self.assertEqual(premium_days_left(self.andre), 14)


# ---------------------------------------------------------------- Lumen tokens (Adapty virtual currency)

class TokenTests(unittest.TestCase):
    def setUp(self):
        fresh_state(self)
        server._tokens_off["until"] = 0.0
        join("Andre", "andre@example.com")
        self.andre = cuid("andre@example.com")

    def balance(self, c):
        return server.token_wallet(c)["balance"]

    def test_new_user_gets_welcome_tokens_once(self):
        self.assertEqual(self.balance(self.andre), server.WELCOME_TOKENS)
        join("Andre", "ANDRE@example.com")  # signing in again
        self.assertEqual(self.balance(self.andre), server.WELCOME_TOKENS)

    def test_spending_takes_the_price_and_never_goes_below_zero(self):
        r = server.spend_tokens(self.andre, "film_noir")
        self.assertEqual((r["spent"], r["balance"]), (5, 5))
        with self.assertRaises(server.Problem):
            server.spend_tokens(self.andre, "export_full")  # costs 8, has 5
        self.assertEqual(self.balance(self.andre), 5, "a refused spend changes nothing")
        with self.assertRaises(server.Problem):
            server.spend_tokens(self.andre, "free_lunch")

    def test_premium_users_are_not_charged(self):
        server.purchase(self.andre, "monthly")
        self.assertEqual(server.spend_tokens(self.andre, "film_noir")["spent"], 0)
        self.assertEqual(self.balance(self.andre), server.WELCOME_TOKENS)

    def test_token_pack_adds_tokens_but_is_not_a_subscription(self):
        r = server.buy_tokens(self.andre, "tokens_30")
        self.assertEqual(r["balance"], server.WELCOME_TOKENS + 30)
        self.assertFalse(server.ADAPTY.has_paid_subscription(server.profile(self.andre)))
        self.assertIsNone(server.premium_until(server.profile(self.andre)))
        with self.assertRaises(server.Problem):
            server.buy_tokens(self.andre, "tokens_1000000")

    def test_token_pack_does_not_count_as_a_referral_payment(self):
        join("Mila", "mila@example.com", ref=code("Andre", "andre@example.com"))
        server.buy_tokens(cuid("mila@example.com"), "tokens_30")
        self.assertEqual(server.num(attrs(self.andre), "referrals_paid"), 0)

    def test_token_rules_are_validated(self):
        rule = {"trigger": "first_payment", "recipient": "inviter", "action": "tokens"}
        for bad in (0, 1001):
            with self.assertRaises(server.Problem):
                server.validate_program({**server.DEFAULT_PROGRAM, "rules": [{**rule, "amount": bad}]})
        p = server.validate_program({**server.DEFAULT_PROGRAM, "rules": [{**rule, "amount": 25}]})
        self.assertEqual(p["rules"][0]["amount"], 25)

    def test_token_rule_rewards_the_inviter_within_the_cap(self):
        server.save_program({**server.DEFAULT_PROGRAM, "max_rewarded_referrals": 1, "rules": [
            {"trigger": "first_payment", "recipient": "inviter", "action": "tokens", "amount": 25}]})
        for n in (1, 2):
            join("Friend", f"friend{n}@example.com", ref=code("Andre", "andre@example.com"))
            server.purchase(cuid(f"friend{n}@example.com"), "monthly")
        self.assertEqual(self.balance(self.andre), server.WELCOME_TOKENS + 25, "the cap allows one reward")
        self.assertEqual(attrs(self.andre)["referrals_paid"], 2)

    def test_one_users_404_never_switches_tokens_off_for_everyone(self):
        server.tokens_unavailable("http_404")
        server.tokens_unavailable("http_403")
        self.assertTrue(server.tokens_on())
        self.assertEqual(server.token_wallet("lumen_ffffffff")["balance"], 0, "no balance yet reads as 0")
        self.assertTrue(server.tokens_on())

    def test_paid_but_not_credited_pack_is_logged_as_owed(self):
        server.ADAPTY.change_tokens = lambda *a, **k: (False, "rate_limited")
        with self.assertRaises(server.Problem):
            server.buy_tokens(self.andre, "tokens_30")
        with server.db() as c:
            owed = c.execute("SELECT text FROM events WHERE kind = 'owed'").fetchall()
        self.assertEqual(len(owed), 1)
        self.assertIn("transaction", owed[0][0])
        self.assertIn("30 tokens", owed[0][0])

    def test_tokens_switch_off_when_adapty_has_no_currency(self):
        server.tokens_unavailable("unknown_currency")
        self.assertIsNone(server.token_wallet(self.andre))
        server._tokens_off["until"] = 0.0
        self.assertIsNotNone(server.token_wallet(self.andre))


# ---------------------------------------------------------------- webhook over HTTP

class WebhookTests(unittest.TestCase):
    SECRET = "whsec_test"

    def setUp(self):
        fresh_state(self)
        server.WEBHOOK_SECRET = self.SECRET
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        join("Andre", "andre@example.com")
        self.andre = cuid("andre@example.com")
        join("Mila", "mila@example.com", ref=code("Andre", "andre@example.com"))
        self.mila = cuid("mila@example.com")

    def post(self, path, body, auth=None):
        headers = {"Content-Type": "application/json"}
        if auth is not None:
            headers["Authorization"] = auth
        req = urllib.request.Request(self.base + path, json.dumps(body).encode(), headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status
        except urllib.error.HTTPError as e:
            return e.code

    def wait_for_paid(self, expected, seconds=5.0):
        deadline = time.time() + seconds
        while time.time() < deadline:
            if server.num(attrs(self.andre), "referrals_paid") == expected:
                return True
            time.sleep(0.05)
        return False

    def test_verification_request_is_accepted_without_auth(self):
        self.assertEqual(self.post("/webhooks/adapty", {}), 200)

    def test_wrong_or_missing_authorization_is_rejected(self):
        event = {"event_type": "subscription_started", "customer_user_id": self.mila}
        self.assertEqual(self.post("/webhooks/adapty", event), 401)
        self.assertEqual(self.post("/webhooks/adapty", event, auth="whsec_wrong"), 401)
        time.sleep(0.3)
        self.assertEqual(server.num(attrs(self.andre), "referrals_paid"), 0)

    def test_first_payment_event_rewards_inviter_and_repeats_are_ignored(self):
        server.ADAPTY.set_transaction(self.mila, "monthly")
        event = {"event_type": "subscription_started", "customer_user_id": self.mila}
        self.assertEqual(self.post("/webhooks/adapty", event, auth=self.SECRET), 200)
        self.assertTrue(self.wait_for_paid(1))
        self.assertEqual(self.post("/webhooks/adapty", event, auth=self.SECRET), 200)  # Adapty retries
        time.sleep(0.3)
        self.assertEqual(attrs(self.andre)["referrals_paid"], 1)

    def test_token_pack_purchase_event_does_not_reward(self):
        event = {"event_type": "non_subscription_purchase", "customer_user_id": self.mila,
                 "event_properties": {"vendor_product_id": "lumen.demo.tokens30"}}
        self.assertEqual(self.post("/webhooks/adapty", event, auth=self.SECRET), 200)
        time.sleep(0.3)
        self.assertEqual(server.num(attrs(self.andre), "referrals_paid"), 0)

    def test_other_events_do_not_reward(self):
        event = {"event_type": "subscription_renewed", "customer_user_id": self.mila}
        self.assertEqual(self.post("/webhooks/adapty", event, auth=self.SECRET), 200)
        time.sleep(0.3)
        self.assertEqual(server.num(attrs(self.andre), "referrals_paid"), 0)

    def test_fallback_rewards_when_no_webhook_arrives(self):
        server.WEBHOOK_FALLBACK_SECONDS = 0.1
        self.assertEqual(server.purchase(self.mila, "monthly")["reward"], "pending")
        self.assertTrue(self.wait_for_paid(1))

    def test_admin_api_needs_the_password_when_set(self):
        server.ADMIN_PASSWORD = "s3cret"
        self.addCleanup(setattr, server, "ADMIN_PASSWORD", "")
        self.assertEqual(self.post("/api/reset", {}), 401)
        good = "Basic " + base64.b64encode(b"any:s3cret").decode()
        self.assertEqual(self.post("/api/reset", {}, auth=good), 200)


# ---------------------------------------------------------------- Adapty client quirks

class AdaptyClientTests(unittest.TestCase):
    def test_numbers_are_sent_as_floats(self):
        # Adapty rejects the whole profile write if a number attribute arrives as an int
        sent = {a["key"]: a["value"] for a in AdaptyClient._attrs({"n": 3, "f": 2.5, "s": "x", "b": True})}
        self.assertEqual(sent, {"n": 3.0, "f": 2.5, "s": "x", "b": "yes"})
        self.assertIsInstance(sent["n"], float)

    def test_device_id_is_a_stable_uuid(self):
        self.assertEqual(device_uuid("lumen_0123abcd"), device_uuid("lumen_0123abcd"))
        uuid.UUID(device_uuid("lumen_0123abcd"))

    def test_device_from_user_agent(self):
        self.assertEqual(device_from_ua(ANDROID_UA), {"platform": "Android", "os": "Android 14", "device": "Pixel 8"})
        self.assertEqual(device_from_ua(IOS_UA), {"platform": "iOS", "os": "iOS 17.4", "device": "iPhone"})
        self.assertEqual(device_from_ua("")["platform"], "iOS")

    def test_only_active_store_subscriptions_count_as_paying(self):
        future = (datetime.now(timezone.utc) + timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
        past = (datetime.now(timezone.utc) - timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
        c = AdaptyClient("", "", "main_paywall", "premium", server.REFERRED_PAYWALL)
        self.assertTrue(c.has_paid_subscription({"subscriptions": [{"store_product_id": "lumen.demo.annual", "expires_at": future}]}))
        self.assertFalse(c.has_paid_subscription({"subscriptions": [{"store_product_id": "lumen.demo.annual", "expires_at": past}]}))
        self.assertFalse(c.has_paid_subscription({"subscriptions": [{"store_product_id": "other.app", "expires_at": future}]}))
        self.assertFalse(c.has_paid_subscription({}))


if __name__ == "__main__":
    unittest.main()
