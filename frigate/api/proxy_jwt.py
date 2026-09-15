"""Validate signed identity from an explicitly configured authentication proxy."""

import base64
import json
import threading
import time
from dataclasses import dataclass

import requests
from joserfc import jwt
from joserfc.errors import JoseError
from joserfc.jwk import KeySet, OctKey

from frigate.config.proxy import ProxyJwtConfig

JWKS_TTL_SECONDS = 300
MAX_JWKS_BYTES = 65536
_lock = threading.Lock()
_cache: dict[str, tuple[float, KeySet]] = {}


@dataclass(frozen=True)
class ProxyIdentity:
    username: str
    groups: tuple[str, ...]


def _get_keys(url: str) -> KeySet:
    # Bound both retrieval and cache lifetime. Never use an attacker-supplied
    # jku/x5u header, redirect target, or stale keys following refresh failure.
    with _lock:
        now = time.monotonic()
        cached = _cache.get(url)
        if cached and now < cached[0]:
            return cached[1]
        with requests.get(
            url, timeout=(3, 3), allow_redirects=False, stream=True
        ) as response:
            if response.status_code != 200:
                raise ValueError("Signing keys unavailable")
            body = bytearray()
            for chunk in response.iter_content(chunk_size=4096):
                body.extend(chunk)
                if len(body) > MAX_JWKS_BYTES:
                    raise ValueError("Signing keys response too large")
        document = json.loads(body)
        if not isinstance(document, dict):
            raise ValueError("Invalid signing key document")
        keys = document.get("keys", [])
        if not isinstance(keys, list) or not keys or len(keys) > 16:
            raise ValueError("Invalid signing key inventory")
        if any(not isinstance(key, dict) for key in keys):
            raise ValueError("Invalid signing key inventory")
        # Public RSA signing keys only. Reject private and symmetric material.
        if any(
            key.get("kty") != "RSA"
            or key.get("use", "sig") != "sig"
            or key.get("alg", "RS256") != "RS256"
            or any(name in key for name in ("d", "p", "q", "k"))
            for key in keys
        ):
            raise ValueError("Invalid signing key type")
        for key in keys:
            modulus = int.from_bytes(base64.urlsafe_b64decode(key["n"] + "=="), "big")
            if modulus.bit_length() < 2048:
                raise ValueError("RSA signing key must be at least 2048 bits")
        keyset = KeySet.import_key_set(document)
        if len(_cache) >= 8:
            _cache.clear()
        _cache[url] = (time.monotonic() + JWKS_TTL_SECONDS, keyset)
        return keyset


def verify_proxy_identity(encoded: str, config: ProxyJwtConfig) -> ProxyIdentity | None:
    """Verify signature, issuer, audience, lifetime, and typed identity claims."""
    if not encoded or len(encoded) > 32768:
        return None
    try:
        if config.algorithm == "HS256":
            # Read only the operator-configured mount, never a token header URL
            # or key. Re-read for rotation and fail closed on missing mounts.
            with open(config.secret_file, "rb") as secret_file:
                secret = secret_file.read(4097)
            if not 32 <= len(secret) <= 4096 or b"-----BEGIN" in secret:
                return None
            key = OctKey.import_key(secret)
        else:
            key = _get_keys(config.jwks_url)
        token = jwt.decode(encoded, key, algorithms=[config.algorithm])
        claims = token.claims
        jwt.JWTClaimsRegistry(
            leeway=0,
            iss={"essential": True, "value": config.issuer},
            aud={"essential": True, "value": config.audience},
            exp={"essential": True},
            iat={"essential": True},
            sub={"essential": True},
        ).validate(claims)
        # Accept exactly this application audience, including its singleton
        # array representation. Multi-audience tokens are deliberately refused.
        if claims["aud"] not in (config.audience, [config.audience]):
            return None
        if not isinstance(claims["sub"], str) or not claims["sub"]:
            return None
        username = claims.get("preferred_username")
        groups = claims.get("groups")
        if (
            not isinstance(username, str)
            or not username.strip()
            or len(username) > 256
            or any(ord(char) < 32 or ord(char) > 126 for char in username)
            or not isinstance(groups, list)
            or len(groups) > 256
            or any(not isinstance(group, str) for group in groups)
        ):
            return None
        return ProxyIdentity(username, tuple(groups))
    except (
        JoseError,
        ValueError,
        TypeError,
        KeyError,
        OSError,
        requests.RequestException,
    ):
        # Never log the token, claims, or upstream response bodies.
        return None
