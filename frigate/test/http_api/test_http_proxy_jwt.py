"""Cryptographic proxy identity and native integration boundary tests."""

import json
import time
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient
from joserfc import jwt
from joserfc.jwk import RSAKey
from pydantic import ValidationError

from frigate.api import auth as auth_module
from frigate.api import proxy_jwt
from frigate.config.proxy import ProxyConfig, ProxyJwtConfig
from frigate.test.http_api import test_http_metrics_auth as metrics

ISSUER = "https://auth.example/application/o/frigate/"
JWKS_URL = ISSUER + "jwks/"
AUDIENCE = "frigate-client"


class TestHttpProxyJwt(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.key = RSAKey.generate_key(2048, parameters={"kid": "trusted"})
        cls.other_key = RSAKey.generate_key(2048, parameters={"kid": "other"})

    def setUp(self):
        metrics.TestHttpMetricsAuth.setUp(self)
        proxy_jwt._cache.clear()
        self.http = patch.object(proxy_jwt.requests, "get").start()
        self.addCleanup(patch.stopall)
        self.response = MagicMock(status_code=200)
        self.http.return_value.__enter__.return_value = self.response
        self.response.iter_content.return_value = [
            json.dumps({"keys": [self.key.as_dict(private=False)]}).encode()
        ]

    def _create_app(self):
        app = metrics.TestHttpMetricsAuth._create_app(self)
        app.frigate_config.proxy = ProxyConfig(
            jwt={"issuer": ISSUER, "audience": AUDIENCE, "jwks_url": JWKS_URL},
            header_map={
                "user": "x-authentik-username",
                "role": "x-authentik-groups",
                "role_map": {"admin": ["Frigate Admins"]},
            },
            separator="|",
        )
        return app

    def claims(self):
        now = int(time.time())
        return {
            "iss": ISSUER,
            "aud": AUDIENCE,
            "sub": "immutable-user-id",
            "iat": now,
            "exp": now + 300,
            "preferred_username": "local-user",
            "groups": ["family"],
        }

    def token(self, claims=None, key=None):
        return jwt.encode(
            {"alg": "RS256", "kid": "trusted"}, claims or self.claims(), key or self.key
        )

    def request(self, token=None, *, extra=None, cookie=None):
        headers = {
            "x-server-port": "8971",
            "x-original-url": "https://cameras.example/api/profile",
            "x-original-method": "GET",
        }
        if token is not None:
            headers["x-authentik-jwt"] = token
        headers.update(extra or {})
        with TestClient(self.app) as client:
            if cookie:
                client.cookies.set("frigate_token", cookie)
            return client.get("/auth", headers=headers)

    def native_token(self, expires_in=3600):
        return auth_module.create_encoded_jwt(
            "homeassistant", "admin", int(time.time()) + expires_in, self.app.jwt_token
        )

    def test_verified_local_user_is_viewer_without_native_account(self):
        response = self.request(self.token())
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.headers["remote-user"], "local-user")
        self.assertEqual(response.headers["remote-role"], "viewer")
        self.assertNotIn("set-cookie", response.headers)

    def test_admin_mapping_uses_signed_groups(self):
        claims = self.claims()
        claims["groups"].append("Frigate Admins")
        self.assertEqual(
            self.request(self.token(claims)).headers["remote-role"], "admin"
        )

    def test_forged_headers_cannot_override_signed_viewer(self):
        response = self.request(
            self.token(),
            extra={
                "x-authentik-username": "admin",
                "x-authentik-groups": "Frigate Admins",
                "remote-role": "admin",
            },
        )
        self.assertEqual(response.headers["remote-user"], "local-user")
        self.assertEqual(response.headers["remote-role"], "viewer")

    def test_signed_viewer_wins_over_old_native_admin_cookie_and_bearer(self):
        response = self.request(
            self.token(),
            cookie=self.native_token(),
            extra={"authorization": "Bearer " + self.native_token()},
        )
        self.assertEqual(response.headers["remote-role"], "viewer")

    def test_invalid_signed_identity_never_falls_back_to_native_cookie(self):
        for token in ["", "invalid", self.token(key=self.other_key)]:
            with self.subTest(token_length=len(token)):
                self.assertEqual(
                    self.request(token, cookie=self.native_token()).status_code, 401
                )

    def test_anonymous_and_forged_headers_are_rejected(self):
        self.assertEqual(self.request().status_code, 401)
        self.assertEqual(
            self.request(extra={"x-authentik-groups": "Frigate Admins"}).status_code,
            401,
        )

    def test_wrong_missing_expired_or_malformed_claims_are_rejected(self):
        now = int(time.time())
        changes = [
            ("iss", "https://other.example/"),
            ("aud", "another-app"),
            ("aud", [AUDIENCE, "another-app"]),
            ("exp", now - 1),
            ("iat", now + 60),
            ("nbf", now + 60),
            ("sub", ""),
            ("preferred_username", ""),
            ("preferred_username", "line\nbreak"),
            ("groups", "Frigate Admins"),
            ("groups", [1]),
            ("groups", ["family|Frigate Admins"]),
        ]
        for name, value in changes:
            with self.subTest(claim=name, value=value):
                claims = self.claims()
                claims[name] = value
                self.assertEqual(self.request(self.token(claims)).status_code, 401)
        for name in ["iss", "aud", "exp", "iat", "sub", "groups", "preferred_username"]:
            with self.subTest(missing=name):
                claims = self.claims()
                del claims[name]
                self.assertEqual(self.request(self.token(claims)).status_code, 401)

    def test_native_bearer_and_refresh_work_without_proxy_jwt(self):
        self.assertEqual(
            self.request(
                extra={"authorization": "Bearer " + self.native_token()}
            ).status_code,
            202,
        )
        with patch.object(
            auth_module.User,
            "get_by_id",
            return_value=SimpleNamespace(password_changed_at=None),
        ):
            response = self.request(cookie=self.native_token(30))
        self.assertEqual(response.status_code, 202)
        self.assertIn("HttpOnly", response.headers["set-cookie"])
        self.http.assert_not_called()

    def test_metrics_credential_remains_scoped_without_proxy_jwt(self):
        self.token_file.write_text(metrics.TOKEN_A + "\n")
        response = metrics.TestHttpMetricsAuth._authenticate(self, metrics.TOKEN_A)
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.headers["remote-role"], "metrics")
        self.assertEqual(
            self.request(
                extra={"authorization": "Bearer " + metrics.TOKEN_A}
            ).status_code,
            401,
        )
        self.http.assert_not_called()

    def test_jwks_cache_and_fixed_tls_endpoint(self):
        token = self.token()
        self.assertEqual(self.request(token).status_code, 202)
        self.assertEqual(self.request(token).status_code, 202)
        self.http.assert_called_once_with(
            JWKS_URL, timeout=(3, 3), allow_redirects=False, stream=True
        )

    def test_unavailable_redirected_oversized_keys_fail_closed(self):
        for code in [301, 302, 500]:
            self.response.status_code = code
            self.assertEqual(self.request(self.token()).status_code, 401)
        self.response.status_code = 200
        self.response.iter_content.return_value = [
            b"a" * (proxy_jwt.MAX_JWKS_BYTES + 1)
        ]
        self.assertEqual(self.request(self.token()).status_code, 401)

    def test_expired_cache_never_survives_refresh_failure(self):
        self.assertEqual(self.request(self.token()).status_code, 202)
        _, keys = proxy_jwt._cache[JWKS_URL]
        proxy_jwt._cache[JWKS_URL] = (0, keys)
        self.response.status_code = 503
        self.assertEqual(self.request(self.token()).status_code, 401)

    def test_loopback_health_bypasses_proxy_and_native_auth(self):
        response = self.request(extra={"x-server-port": "5000"})
        self.assertEqual(response.status_code, 202)
        self.http.assert_not_called()

    def test_disabled_native_auth_with_signed_proxy_is_fail_closed(self):
        self.app.frigate_config.auth.enabled = False
        self.assertEqual(self.request(self.token()).status_code, 401)

    def test_media_and_stream_authorization_remain_enforced(self):
        for helper in [
            "deny_response_for_media_uri",
            "deny_response_for_go2rtc_stream",
        ]:
            with (
                self.subTest(helper=helper),
                patch.object(auth_module, helper, return_value=403),
            ):
                self.assertEqual(self.request(self.token()).status_code, 403)

    def test_malformed_or_private_jwks_are_rejected(self):
        documents = [
            [],
            {"keys": [1]},
            {"keys": "bad"},
            {"keys": []},
            {"keys": [self.key.as_dict(private=True)]},
            {"keys": [{"kty": "oct", "k": "anything"}]},
        ]
        for document in documents:
            with self.subTest(document_type=type(document).__name__):
                proxy_jwt._cache.clear()
                self.response.iter_content.return_value = [
                    json.dumps(document).encode()
                ]
                self.assertEqual(self.request(self.token()).status_code, 401)

    def test_rsa_signature_algorithm_cannot_be_downgraded(self):
        token = jwt.encode({"alg": "RS512", "kid": "trusted"}, self.claims(), self.key)
        self.assertEqual(self.request(token).status_code, 401)

    def test_tls_failure_rejects_proxy_but_keeps_native_access(self):
        self.http.side_effect = proxy_jwt.requests.exceptions.SSLError(
            "certificate verification failed"
        )
        self.assertEqual(self.request(self.token()).status_code, 401)
        self.assertEqual(
            self.request(
                extra={"authorization": "Bearer " + self.native_token()}
            ).status_code,
            202,
        )

    def test_configuration_requires_https_pinned_endpoints(self):
        for url in [
            "http://auth.example/keys",
            "https://user:pass@auth.example/keys",
            "https://auth.example/keys#fragment",
        ]:
            with self.subTest(url=url), self.assertRaises(ValidationError):
                ProxyJwtConfig(issuer=ISSUER, audience=AUDIENCE, jwks_url=url)
