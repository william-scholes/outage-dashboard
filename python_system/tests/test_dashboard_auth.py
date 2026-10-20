"""Unit tests for the dashboard authentication (HTTP Basic Auth & Form Login / Session Cookies)."""
from __future__ import annotations

import base64
import json
import os
import shutil
import socket
import tempfile
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from outage_automation.dashboard.server import (
    InsecureBindError,
    SESSION_COOKIE_NAME,
    _build_clear_cookie,
    _build_session_cookie,
    _derive_session_secret,
    _generate_session_token,
    _verify_session_token,
    run_server,
)
from outage_automation.paths import resolve_paths


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class TestSessionTokenLogic(unittest.TestCase):
    def setUp(self) -> None:
        self.auth = ("admin", "secret123")
        self.secret = _derive_session_secret(self.auth)

    def test_token_lifecycle(self) -> None:
        token = _generate_session_token("admin", self.secret)
        self.assertTrue(_verify_session_token(token, self.secret, "admin"))

    def test_token_fails_with_different_user(self) -> None:
        token = _generate_session_token("admin", self.secret)
        self.assertFalse(_verify_session_token(token, self.secret, "alice"))

    def test_token_fails_after_password_change(self) -> None:
        token = _generate_session_token("admin", self.secret)
        new_secret = _derive_session_secret(("admin", "different_secret_456"))
        self.assertFalse(_verify_session_token(token, new_secret, "admin"))

    def test_tampered_token_rejected(self) -> None:
        token = _generate_session_token("admin", self.secret)
        parts = token.split(".")
        # Tamper signature
        bad_sig = parts[0] + "." + parts[1] + "." + ("0" * len(parts[2]))
        self.assertFalse(_verify_session_token(bad_sig, self.secret, "admin"))
        # Tamper payload
        bad_payload = "Ym9ndXM." + parts[1] + "." + parts[2]
        self.assertFalse(_verify_session_token(bad_payload, self.secret, "admin"))

    def test_expired_token_rejected(self) -> None:
        token = _generate_session_token("admin", self.secret)
        # Verify with max_age = -1s -> should be expired
        self.assertFalse(_verify_session_token(token, self.secret, "admin", max_age=-1))


class TestDashboardAuth(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="test_auth_"))
        self.paths = resolve_paths(str(self.tmp))
        self.paths.state_dir.mkdir(parents=True, exist_ok=True)
        # Initialize empty files
        (self.paths.state_dir / "alert_history.json").write_text("[]", encoding="utf-8")
        (self.paths.state_dir / "customers.json").write_text("[]", encoding="utf-8")
        self.port = _free_port()
        self.auth = ("admin", "supersecret123")
        self.server = run_server(
            paths=self.paths,
            host="127.0.0.1",
            port=self.port,
            auth=self.auth,
            block=False,
        )

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _request(
        self,
        path: str,
        auth_tuple: tuple[str, str] | None = None,
        cookie: str | None = None,
        follow_redirects: bool = True,
        method: str = "GET",
        data: bytes | None = None,
        content_type: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict, bytes]:
        url = f"http://127.0.0.1:{self.port}{path}"
        req = urllib.request.Request(url, data=data, method=method)
        if auth_tuple:
            token = base64.b64encode(f"{auth_tuple[0]}:{auth_tuple[1]}".encode("utf-8")).decode("ascii")
            req.add_header("Authorization", f"Basic {token}")
        if cookie:
            req.add_header("Cookie", cookie)
        if content_type:
            req.add_header("Content-Type", content_type)
        if headers:
            for k, v in headers.items():
                req.add_header(k, v)

        opener = urllib.request.build_opener() if follow_redirects else urllib.request.build_opener(_NoRedirectHandler)
        try:
            with opener.open(req, timeout=5) as resp:
                hdrs = dict(resp.headers)
                return resp.status, hdrs, resp.read()
        except urllib.error.HTTPError as e:
            hdrs = dict(e.headers)
            return e.code, hdrs, e.read()

    def test_health_endpoint_open_without_auth(self) -> None:
        code, _, body = self._request("/api/health")
        self.assertEqual(code, 200)
        data = json.loads(body.decode("utf-8"))
        self.assertEqual(data.get("status"), "ok")

    def test_favicon_endpoint_open_without_auth(self) -> None:
        code, headers, body = self._request("/favicon.ico")
        self.assertEqual(code, 200)
        self.assertIn("image/x-icon", headers.get("Content-Type", ""))
        self.assertGreater(len(body), 0)

    def test_api_dashboard_401_without_creds(self) -> None:
        code, headers, _ = self._request("/api/dashboard")
        self.assertEqual(code, 401)
        auth_header = headers.get("WWW-Authenticate") or headers.get("www-authenticate")
        self.assertTrue(auth_header)
        self.assertIn("Basic", auth_header)

    def test_api_dashboard_401_with_bad_creds(self) -> None:
        code, _, _ = self._request("/api/dashboard", ("admin", "wrongpassword"))
        self.assertEqual(code, 401)

    def test_api_dashboard_200_with_valid_creds(self) -> None:
        code, _, body = self._request("/api/dashboard", self.auth)
        self.assertEqual(code, 200)
        data = json.loads(body.decode("utf-8"))
        self.assertIn("rows", data)

    def test_static_asset_protected(self) -> None:
        code, _, _ = self._request("/static/app.css")
        self.assertEqual(code, 401)
        code_ok, _, _ = self._request("/static/app.css", self.auth)
        self.assertEqual(code_ok, 200)

    def test_insecure_bind_rejected(self) -> None:
        port = _free_port()
        with self.assertRaises(InsecureBindError):
            run_server(
                paths=self.paths,
                host="0.0.0.0",
                port=port,
                auth=None,
                allow_insecure_bind=False,
                block=False,
            )

    # --- New tests for Login Page, Cookies, and Logout flow ---

    def test_login_page_renders_form(self) -> None:
        code, headers, body = self._request("/login")
        self.assertEqual(code, 200)
        text = body.decode("utf-8")
        self.assertIn('<form method="POST" action="/login">', text)
        self.assertIn('name="username"', text)
        self.assertIn('name="password"', text)
        self.assertIn('autocomplete="username"', text)
        self.assertIn('autocomplete="current-password"', text)
        self.assertIn('Sign In', text)

    def test_unauthenticated_browser_page_redirects_to_login(self) -> None:
        code, headers, _ = self._request("/dashboard", follow_redirects=False)
        self.assertEqual(code, 302)
        location = headers.get("Location")
        self.assertTrue(location)
        self.assertIn("/login", location)
        self.assertIn("next=/dashboard", location)

    def test_root_redirects(self) -> None:
        # Unauthenticated: redirects to login
        code, headers, _ = self._request("/", follow_redirects=False)
        self.assertEqual(code, 302)
        self.assertEqual(headers.get("Location"), "/login?next=/dashboard")

        # Authenticated with session cookie: redirects to /dashboard
        secret = _derive_session_secret(self.auth)
        token = _generate_session_token("admin", secret)
        code_auth, headers_auth, _ = self._request(
            "/",
            cookie=f"{SESSION_COOKIE_NAME}={token}",
            follow_redirects=False,
        )
        self.assertEqual(code_auth, 302)
        self.assertEqual(headers_auth.get("Location"), "/dashboard")

    def test_login_post_bad_creds_shows_error(self) -> None:
        form_data = urllib.parse.urlencode({
            "username": "admin",
            "password": "wrongpassword",
            "next": "/dashboard",
        }).encode("utf-8")
        code, _, body = self._request(
            "/login",
            method="POST",
            data=form_data,
            content_type="application/x-www-form-urlencoded",
        )
        self.assertEqual(code, 401)
        text = body.decode("utf-8")
        self.assertIn("Invalid username or password", text)

    def test_login_post_valid_creds_sets_cookie_and_redirects(self) -> None:
        form_data = urllib.parse.urlencode({
            "username": "admin",
            "password": "supersecret123",
            "next": "/dashboard",
        }).encode("utf-8")
        code, headers, _ = self._request(
            "/login",
            method="POST",
            data=form_data,
            content_type="application/x-www-form-urlencoded",
            follow_redirects=False,
        )
        self.assertEqual(code, 302)
        self.assertEqual(headers.get("Location"), "/dashboard")
        set_cookie = headers.get("Set-Cookie", "")
        self.assertIn(f"{SESSION_COOKIE_NAME}=", set_cookie)
        self.assertIn("HttpOnly", set_cookie)
        self.assertIn("SameSite=Lax", set_cookie)

    def test_authenticated_via_cookie(self) -> None:
        # 1. Login to get cookie
        form_data = urllib.parse.urlencode({
            "username": "admin",
            "password": "supersecret123",
            "next": "/dashboard",
        }).encode("utf-8")
        code, headers, _ = self._request(
            "/login",
            method="POST",
            data=form_data,
            content_type="application/x-www-form-urlencoded",
            follow_redirects=False,
        )
        set_cookie = headers.get("Set-Cookie", "")
        cookie_val = set_cookie.split(";")[0]

        # 2. Visit /dashboard with cookie
        code_dash, _, body_dash = self._request("/dashboard", cookie=cookie_val, follow_redirects=False)
        self.assertEqual(code_dash, 200)
        self.assertIn("Outage Dashboard", body_dash.decode("utf-8"))

        # 3. Visit /api/dashboard with cookie
        code_api, _, body_api = self._request("/api/dashboard", cookie=cookie_val)
        self.assertEqual(code_api, 200)
        data = json.loads(body_api.decode("utf-8"))
        self.assertIn("rows", data)

    def test_logout_clears_cookie(self) -> None:
        code, headers, _ = self._request("/logout", follow_redirects=False)
        self.assertEqual(code, 302)
        self.assertEqual(headers.get("Location"), "/login")
        set_cookie = headers.get("Set-Cookie", "")
        self.assertIn("Max-Age=0", set_cookie)

    def test_json_login_and_logout(self) -> None:
        login_json = json.dumps({"username": "admin", "password": "supersecret123"}).encode("utf-8")
        code, headers, body = self._request(
            "/login",
            method="POST",
            data=login_json,
            content_type="application/json",
            headers={"Accept": "application/json"},
        )
        self.assertEqual(code, 200)
        data = json.loads(body.decode("utf-8"))
        self.assertEqual(data.get("status"), "ok")
        cookie_val = headers.get("Set-Cookie", "").split(";")[0]

        # JSON logout
        code_out, headers_out, body_out = self._request(
            "/logout",
            method="POST",
            cookie=cookie_val,
            headers={"Accept": "application/json"},
        )
        self.assertEqual(code_out, 200)
        data_out = json.loads(body_out.decode("utf-8"))
        self.assertEqual(data_out.get("status"), "ok")
        self.assertIn("Max-Age=0", headers_out.get("Set-Cookie", ""))

    def test_tampered_cookie_redirects_to_login(self) -> None:
        code, headers, _ = self._request(
            "/dashboard",
            cookie=f"{SESSION_COOKIE_NAME}=bogus_payload.12345.bogus_sig",
            follow_redirects=False,
        )
        self.assertEqual(code, 302)
        self.assertIn("/login", headers.get("Location", ""))

    def test_brand_title_configuration(self) -> None:
        from outage_automation.config import get_brand_title
        # Default
        self.assertEqual(get_brand_title(), "Outage Dashboard")

        # Env OA_BRAND_TITLE override
        os.environ["OA_BRAND_TITLE"] = "Acme Operations"
        try:
            self.assertEqual(get_brand_title(), "Acme Operations")
        finally:
            os.environ.pop("OA_BRAND_TITLE", None)

        # Env OA_BRAND_NAME override
        os.environ["OA_BRAND_NAME"] = "TIG"
        try:
            self.assertEqual(get_brand_title(), "TIG Outage Dashboard")
        finally:
            os.environ.pop("OA_BRAND_NAME", None)

        # File-based branding.json override
        branding_file = self.paths.state_dir / "branding.json"
        branding_file.write_text(json.dumps({"brand_title": "Custom NOC Display"}), encoding="utf-8")
        try:
            self.assertEqual(get_brand_title(self.paths), "Custom NOC Display")
        finally:
            branding_file.unlink(missing_ok=True)



if __name__ == "__main__":
    unittest.main()
