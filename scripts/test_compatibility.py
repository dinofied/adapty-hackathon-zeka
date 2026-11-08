"""Local compatibility regressions. Run: python scripts/test_compatibility.py."""

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
        for name in ("app.html", "invite.html", "dashboard.html", "assetlinks.json", "sw.js"):
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

    def request(self, path, body=None, token=None):
        request = urllib.request.Request(
            self.base + path,
            data=json.dumps(body).encode("utf-8") if body is not None else None,
            headers={"Content-Type": "application/json", "X-Token": token or ""},
        )
        try:
            response = urllib.request.urlopen(request, timeout=10)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            return response.status, response.read().decode("utf-8")

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
