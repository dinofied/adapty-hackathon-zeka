"""Local compatibility regressions. Run: python scripts/test_compatibility.py."""

import hashlib
import hmac
import json
from http.server import BaseHTTPRequestHandler, HTTPServer
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]


class CompatibilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temp.cleanup)
        cls.folder = Path(cls.temp.name) / "zeka-\u017e"
        cls.folder.mkdir()
        for name in ("server.py", "adapty_client.py"):
            shutil.copy2(ROOT / name, cls.folder / name)
        (cls.folder / "static").mkdir()
        for name in ("app.html", "invite.html", "dashboard.html", "assetlinks.json", "sw.js", "app-launch.js"):
            shutil.copy2(ROOT / "static" / name, cls.folder / "static" / name)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        cls.base = f"http://127.0.0.1:{port}"
        cls.label = "\u0416\u2603"
        (cls.folder / ".env").write_text(
            'ASSETLINKS_JSON=' + json.dumps([{"label": cls.label}], ensure_ascii=False)
            + "\nADAPTY_SECRET_KEY=\nADMIN_PASSWORD=\n",
            encoding="utf-8-sig",
        )
        cls.env = {k: v for k, v in os.environ.items()
                   if not k.startswith("ADAPTY_") and k not in (
                       "ADMIN_PASSWORD", "WEBHOOK_SECRET", "APK_URL", "ASSETLINKS_JSON", "DATA_DIR")}
        cls.env.update(PORT=str(port), DATA_DIR=str(cls.folder),
                       PYTHONUTF8="0", PYTHONIOENCODING="cp1252:strict")
        cls.log = (cls.folder / "server.log").open("wb")
        cls.addClassCleanup(cls.log.close)
        cls.process = subprocess.Popen(
            [sys.executable, str(cls.folder / "server.py")],
            cwd=cls.temp.name, env=cls.env, stdout=cls.log, stderr=cls.log,
        )
        cls.addClassCleanup(cls.stop_server)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if cls.process.poll() is not None:
                raise RuntimeError(cls.server_log())
            try:
                with urllib.request.urlopen(cls.base + "/healthz", timeout=1) as response:
                    health = json.load(response)
                if health.get("ok") and health.get("mode") == "mock":
                    break
            except (OSError, urllib.error.URLError):
                time.sleep(0.1)
        else:
            raise RuntimeError("Mock server did not become ready.\n" + cls.server_log())

    @classmethod
    def stop_server(cls):
        if cls.process.poll() is None:
            cls.process.terminate()
        try:
            cls.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            cls.process.kill()
            cls.process.wait(timeout=5)

    @classmethod
    def server_log(cls):
        return (cls.folder / "server.log").read_text(encoding="cp1252", errors="replace")

    def request(self, path, body=None, token=None, headers=None):
        request = urllib.request.Request(
            self.base + path,
            data=json.dumps(body).encode("utf-8") if body is not None else None,
            headers={"Content-Type": "application/json", "X-Token": token or "", **(headers or {})},
        )
        try:
            response = urllib.request.urlopen(request, timeout=10)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            self.response_headers = response.headers
            return response.status, response.read().decode("utf-8")

    def inviter(self, label):
        status, body = self.request("/api/app/signup", {"name": "Ana", "email": f"{label}.inviter@example.com"})
        self.assertEqual(status, 200, body)
        token = json.loads(body)["token"]
        status, body = self.request("/api/app/me", token=token)
        self.assertEqual(status, 200, body)
        return json.loads(body)["code"], token

    def invitation_cookie(self, code):
        status, body = self.request("/r/" + code)
        self.assertEqual(status, 200, body)
        return next(value.split(";")[0] for value in self.response_headers.get_all("Set-Cookie")
                    if value.startswith("lumen_pending_ref="))

    def test_install_handoff_applies_referral_and_refreshes_dashboard(self):
        code, inviter_token = self.inviter("handoff")
        cookie = self.invitation_cookie(code)
        status, body = self.request("/app", headers={"Cookie": cookie})
        self.assertEqual(status, 200, body)
        status, body = self.request("/api/app/pending-ref", headers={"Cookie": cookie})
        self.assertEqual(json.loads(body)["code"], code)
        status, body = self.request("/api/state")
        self.assertEqual(status, 200, body)
        status, body = self.request("/api/app/signup", {
            "name": "New friend", "email": "handoff.friend@example.com", "ref": None,
        }, headers={"Cookie": cookie})
        self.assertEqual(status, 200, body)
        self.assertIn("Max-Age=0", self.response_headers["Set-Cookie"])
        friend_token = json.loads(body)["token"]
        status, body = self.request("/api/app/me", token=friend_token)
        self.assertEqual(json.loads(body)["attributes"]["referred_by"], code)
        status, body = self.request("/api/state")
        row = next(row for row in json.loads(body)["users"] if row["email"] == "handoff.friend@example.com")
        self.assertEqual(row["attributes"]["referral_status"], "referred")
        self.assertEqual(row["referred_by_name"], "Ana")
        status, body = self.request("/api/app/signup", {
            "name": "New friend", "email": "handoff.friend@example.com",
        }, headers={"Cookie": cookie})
        self.assertEqual(status, 200, body)
        self.assertTrue(json.loads(body)["existing"])
        status, body = self.request("/api/app/me", token=inviter_token)
        self.assertEqual(json.loads(body)["referrals_made"], 1)

    def test_direct_signup_ignores_legacy_cookie_and_shared_ip_click(self):
        code, _ = self.inviter("legacy")
        self.invitation_cookie(code)
        cookie = {"Cookie": f"lumen_ref={code}"}
        status, body = self.request("/api/app/pending-ref", headers=cookie)
        self.assertIsNone(json.loads(body)["code"])
        status, body = self.request("/api/app/signup", {
            "name": "Organic", "email": "legacy.organic@example.com", "ref": None,
        }, headers=cookie)
        self.assertEqual(status, 200, body)
        status, body = self.request("/api/app/me", token=json.loads(body)["token"])
        self.assertEqual(json.loads(body)["attributes"]["referral_status"], "organic")

    def test_pending_invitation_is_authenticated_and_expires(self):
        code, _ = self.inviter("expiry")
        cookie = self.invitation_cookie(code)
        mock_key = hashlib.sha256(b"lumen-token:mock").digest()
        invalid = [cookie[:-1] + ("0" if cookie[-1] != "0" else "1"), "lumen_pending_ref=garbage"]
        for issued in (int(time.time()) - 3601, int(time.time()) + 3600):
            payload = f"{code}.{issued}"
            sig = hmac.new(mock_key, f"referral:{payload}".encode(), hashlib.sha256).hexdigest()[:24]
            invalid.append(f"lumen_pending_ref={payload}.{sig}")
        for value in invalid:
            with self.subTest(value=value):
                status, body = self.request("/api/app/pending-ref", headers={"Cookie": value})
                self.assertEqual(status, 200, body)
                self.assertIsNone(json.loads(body)["code"])
                self.assertIn("Max-Age=0", self.response_headers["Set-Cookie"])

    def test_download_carries_only_valid_explicit_invitation(self):
        code, _ = self.inviter("download")
        apk = self.folder / "static" / "lumen.apk"
        apk.write_bytes(b"mock APK")
        try:
            status, body = self.request("/download/lumen.apk?ref=" + code)
            self.assertEqual((status, body), (200, "mock APK"))
            cookie = self.response_headers["Set-Cookie"].split(";")[0]
            self.assertIn("HttpOnly", self.response_headers["Set-Cookie"])
            status, body = self.request("/api/app/pending-ref", headers={"Cookie": cookie})
            self.assertEqual(json.loads(body)["code"], code)
            for invalid in ("INVALID", ""):
                status, body = self.request("/download/lumen.apk?ref=" + invalid, headers={"Cookie": cookie})
                self.assertEqual((status, body), (200, "mock APK"))
                self.assertIn("Max-Age=0", self.response_headers["Set-Cookie"])
            status, _ = self.request("/r/INVALID", headers={"Cookie": cookie})
            self.assertIn("Max-Age=0", self.response_headers["Set-Cookie"])
        finally:
            apk.unlink()

    def test_explicit_code_wins_and_existing_accounts_are_not_reattributed(self):
        first, _ = self.inviter("first-choice")
        second, _ = self.inviter("second-choice")
        cookie = self.invitation_cookie(first)
        status, body = self.request("/api/app/signup", {
            "name": "Friend", "email": "choice.friend@example.com", "ref": "UNKNOWN-1234ABCD",
        }, headers={"Cookie": cookie})
        self.assertEqual(status, 400, body)
        self.assertIsNone(self.response_headers.get("Set-Cookie"))
        status, body = self.request("/api/app/signup", {
            "name": "Friend", "email": "choice.friend@example.com", "ref": second,
        }, headers={"Cookie": cookie})
        self.assertEqual(status, 200, body)
        token = json.loads(body)["token"]
        status, body = self.request("/api/app/signup", {
            "name": "Friend", "email": "choice.friend@example.com", "ref": first,
        }, headers={"Cookie": cookie})
        self.assertEqual(status, 200, body)
        self.assertTrue(json.loads(body)["existing"])
        self.assertEqual(json.loads(body)["actions"], [])
        status, body = self.request("/api/app/me", token=token)
        self.assertEqual(json.loads(body)["attributes"]["referred_by"], second)
        status, body = self.request("/api/app/pending-ref?clear=1", headers={"Cookie": cookie})
        self.assertIsNone(json.loads(body)["code"])
        self.assertIn("Max-Age=0", self.response_headers["Set-Cookie"])
        status, body = self.request("/api/app/signup", {
            "name": "Declined", "email": "choice.declined@example.com", "ref": None, "use_pending_ref": False,
        }, headers={"Cookie": cookie})
        self.assertEqual(status, 200, body)
        self.assertIsNone(json.loads(body)["referred_by"])

    def test_web_app_changes_change_worker_version(self):
        status, before = self.request("/sw.js")
        self.assertEqual(status, 200, before)
        app = self.folder / "static" / "app.html"
        original = app.read_bytes()
        try:
            app.write_bytes(original + b"\n")
            status, after = self.request("/sw.js")
            self.assertEqual(status, 200, after)
            self.assertNotEqual(before, after)
            self.assertNotIn("{{VERSION}}", after)
        finally:
            app.write_bytes(original)
        status, body = self.request("/static/app-launch.js")
        self.assertEqual(status, 200, body)
        self.assertEqual(body, (ROOT / "static" / "app-launch.js").read_bytes().decode("utf-8"))
        self.assertEqual(self.response_headers["Cache-Control"], "no-store")

    def test_utf8_environment_with_bom(self):
        status, body = self.request("/.well-known/assetlinks.json")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), [{"label": self.label}])

    def test_pages_preserve_utf8(self):
        for route, name in (("/app", "app.html"), ("/dashboard", "dashboard.html")):
            with self.subTest(route=route):
                status, body = self.request(route)
                self.assertEqual(status, 200)
                source = ROOT / "static" / name
                expected = (source.read_bytes().decode("utf-8") if route == "/dashboard"
                            else source.read_text(encoding="utf-8"))
                self.assertEqual(body, expected)
        status, body = self.request("/api/app/signup", {
            "name": self.label, "email": "compat.inviter@example.com",
        })
        self.assertEqual(status, 200)
        token = json.loads(body)["token"]
        request = urllib.request.Request(self.base + "/api/app/me", headers={"X-Token": token})
        with urllib.request.urlopen(request, timeout=10) as response:
            code = json.load(response)["code"]
        status, body = self.request("/app?ref=" + code)
        self.assertEqual(status, 200)
        expected = (ROOT / "static" / "app.html").read_text(encoding="utf-8").replace(
            'href="/manifest.webmanifest"', f'href="/manifest.webmanifest?ref={code}"')
        self.assertEqual(body, expected)
        status, body = self.request("/r/" + code)
        self.assertEqual(status, 200)
        expected = (ROOT / "static" / "invite.html").read_text(encoding="utf-8")
        expected = (expected.replace("{{CODE}}", code).replace("{{NAME}}", self.label)
                    .replace("{{VALID}}", "1").replace("{{APK}}", "0"))
        self.assertEqual(body, expected)

    def test_unicode_error_logging_does_not_drop_response(self):
        status, body = self.request("/api/program", {
            "rules": [{"trigger": "install", "recipient": "friend", "action": "grant", "days": self.label}],
        })
        self.assertEqual(status, 500)
        self.assertIn(self.label, json.loads(body)["error"])
        self.assertIn("\\u0416\\u2603", self.server_log())
        self.assertNotIn("UnicodeEncodeError", self.server_log())
        status, _ = self.request("/healthz")
        self.assertEqual(status, 200)

    def test_referral_smoke_test(self):
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "smoke_test.py"), self.base],
            cwd=self.temp.name, env=self.env, capture_output=True, timeout=60,
        )
        output = (result.stdout + result.stderr).decode("cp1252", errors="replace")
        self.assertEqual(result.returncode, 0, output)
        self.assertIn("ALL CHECKS PASSED", output)

    def test_smoke_test_reports_unicode_failures(self):
        label = self.label

        class UnhealthyHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps({"ok": False, "error": label}).encode("utf-8"))

            def log_message(self, *args):
                pass

        with HTTPServer(("127.0.0.1", 0), UnhealthyHandler) as httpd:
            httpd.timeout = 10
            worker = threading.Thread(target=httpd.handle_request, daemon=True)
            worker.start()
            try:
                result = subprocess.run(
                    [sys.executable, str(ROOT / "scripts" / "smoke_test.py"),
                     f"http://127.0.0.1:{httpd.server_port}"],
                    cwd=self.temp.name, env=self.env, capture_output=True, timeout=15,
                )
            finally:
                worker.join(timeout=10)
        output = (result.stdout + result.stderr).decode("cp1252", errors="replace")
        self.assertEqual(result.returncode, 1, output)
        self.assertIn("FAIL server is up", output)
        self.assertIn("\\u0416\\u2603", output)
        self.assertNotIn("UnicodeEncodeError", output)


if __name__ == "__main__":
    unittest.main()
