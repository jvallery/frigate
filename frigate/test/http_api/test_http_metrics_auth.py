"""Tests for the dedicated metrics bearer authentication boundary."""

import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from joserfc.jwk import OctKey

from frigate.api import auth as auth_module
from frigate.api.auth import create_encoded_jwt

TOKEN_A = "A" * 43
TOKEN_B = "B" * 43
WRONG_TOKEN = "C" * 43


class TestHttpMetricsAuth(unittest.TestCase):
    """Verify that an opaque bearer can authorize only the metrics scrape."""

    def setUp(self):
        self.token_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.token_directory.cleanup)
        self.token_file = Path(self.token_directory.name) / "metrics-tokens"
        token_path_patch = patch.object(
            auth_module, "METRICS_TOKEN_FILE", self.token_file
        )
        token_path_patch.start()
        self.addCleanup(token_path_patch.stop)
        self.app = self._create_app()

    def _create_app(self, *, auth_enabled: bool = True) -> FastAPI:
        app = FastAPI()
        app.include_router(auth_module.router)
        app.frigate_config = SimpleNamespace(
            auth=SimpleNamespace(
                enabled=auth_enabled,
                cookie_name="frigate_token",
                cookie_secure=False,
                refresh_time=1800,
                session_length=86400,
                roles={"admin": [], "viewer": []},
            ),
            proxy=SimpleNamespace(
                auth_secret=None,
                header_map=SimpleNamespace(user=None, role=None, role_map={}),
                default_role="viewer",
                separator=",",
            ),
            cameras={},
        )
        app.auth_internal_port = 5000
        app.jwt_token = OctKey.import_key(b"test-secret") if auth_enabled else None
        return app

    def _authenticate(
        self,
        token: str,
        *,
        app: FastAPI | None = None,
        original_method: str = "GET",
        original_url: str = "https://frigate.example/api/metrics",
        server_port: str = "8971",
    ):
        with TestClient(app or self.app) as client:
            return client.get(
                "/auth",
                headers={
                    "authorization": f"Bearer {token}",
                    "x-original-method": original_method,
                    "x-original-url": original_url,
                    "x-server-port": server_port,
                },
            )

    def test_one_token_authenticates_exact_metrics_request(self):
        self.token_file.write_text(f"{TOKEN_A}\n")

        with patch.object(auth_module, "logger") as auth_logger:
            response = self._authenticate(TOKEN_A)

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.headers["remote-user"], "metrics")
        self.assertEqual(response.headers["remote-role"], "metrics")
        self.assertNotIn("set-cookie", response.headers)
        self.assertEqual(response.content, b"")
        self.assertNotIn(TOKEN_A, repr(auth_logger.method_calls))
        self.assertNotIn(TOKEN_A, repr(response.headers))
        self.assertNotIn(TOKEN_A, response.text)

    def test_two_tokens_compare_every_candidate_without_early_exit(self):
        self.token_file.write_text(f"{TOKEN_A}\n{TOKEN_B}\n")

        with patch.object(
            auth_module.secrets,
            "compare_digest",
            wraps=auth_module.secrets.compare_digest,
        ) as compare_digest:
            response = self._authenticate(TOKEN_A)

        self.assertEqual(response.status_code, 202)
        self.assertEqual(compare_digest.call_count, 2)

    def test_token_file_is_reloaded_for_rotation_without_app_restart(self):
        self.token_file.write_text(TOKEN_A)
        self.assertEqual(self._authenticate(TOKEN_A).status_code, 202)

        self.token_file.write_text(TOKEN_B)
        self.assertEqual(self._authenticate(TOKEN_A).status_code, 401)
        self.assertEqual(self._authenticate(TOKEN_B).status_code, 202)

    def test_missing_unreadable_wrong_and_malformed_credentials_fail_closed(self):
        self.assertEqual(self._authenticate(TOKEN_A).status_code, 401)

        unreadable_path = MagicMock()
        unreadable_path.open.side_effect = OSError("unreadable")
        with patch.object(auth_module, "METRICS_TOKEN_FILE", unreadable_path):
            self.assertEqual(self._authenticate(TOKEN_A).status_code, 401)

        malformed_files = {
            "empty": b"",
            "short": b"A" * 42,
            "padded": b"A" * 43 + b"=",
            "non_ascii": b"A" * 43 + b"\xff",
            "blank_line": b"A" * 43 + b"\n\n" + b"B" * 43,
            "duplicate": b"A" * 43 + b"\n" + b"A" * 43,
            "three_tokens": (b"A" * 43 + b"\n" + b"B" * 43 + b"\n" + b"C" * 43),
            "oversized": b"A" * 259,
        }
        for name, contents in malformed_files.items():
            with self.subTest(name=name):
                self.token_file.write_bytes(contents)
                self.assertEqual(self._authenticate(TOKEN_A).status_code, 401)

        self.token_file.write_text(TOKEN_A)
        self.assertEqual(self._authenticate(WRONG_TOKEN).status_code, 401)
        self.assertEqual(self._authenticate("too-short").status_code, 401)

    def test_configured_token_is_rejected_outside_exact_metrics_request(self):
        self.token_file.write_text(TOKEN_A)
        rejected_requests = (
            ("POST", "https://frigate.example/api/metrics"),
            ("GET", "https://frigate.example/api/metrics?format=text"),
            ("GET", "https://frigate.example/api/metrics/"),
            ("GET", "https://frigate.example/api/config"),
            ("GET", "https://frigate.example/api/stats"),
            ("GET", "https://frigate.example/api/events"),
            ("GET", "https://frigate.example/api/chat"),
            ("GET", "https://frigate.example/api/users"),
        )

        for method, url in rejected_requests:
            with self.subTest(method=method, url=url):
                self.assertEqual(
                    self._authenticate(
                        TOKEN_A, original_method=method, original_url=url
                    ).status_code,
                    401,
                )

    def test_native_jwt_and_internal_port_authentication_are_unchanged(self):
        self.token_file.write_text(TOKEN_A)
        native_jwt = create_encoded_jwt(
            "admin", "admin", int(time.time()) + 300, self.app.jwt_token
        )

        jwt_response = self._authenticate(
            native_jwt,
            original_url="https://frigate.example/api/config",
        )
        self.assertEqual(jwt_response.status_code, 202)
        self.assertEqual(jwt_response.headers["remote-user"], "admin")
        self.assertEqual(jwt_response.headers["remote-role"], "admin")

        internal_response = self._authenticate(
            WRONG_TOKEN,
            original_url="https://frigate.example/api/config",
            server_port="5000",
        )
        self.assertEqual(internal_response.status_code, 202)
        self.assertEqual(internal_response.headers["remote-user"], "anonymous")
        self.assertEqual(internal_response.headers["remote-role"], "admin")

    def test_auth_disabled_proxy_behavior_is_unchanged(self):
        self.token_file.write_text(TOKEN_A)
        auth_disabled_app = self._create_app(auth_enabled=False)

        response = self._authenticate(
            WRONG_TOKEN,
            app=auth_disabled_app,
            original_url="https://frigate.example/api/config",
        )

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.headers["remote-user"], "viewer")
        self.assertEqual(response.headers["remote-role"], "viewer")


if __name__ == "__main__":
    unittest.main(verbosity=2)
