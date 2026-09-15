from urllib.parse import urlsplit

from pydantic import Field, field_validator

from .base import FrigateBaseModel
from .env import EnvString

__all__ = ["ProxyConfig", "HeaderMappingConfig"]


class HeaderMappingConfig(FrigateBaseModel):
    user: str = Field(
        default=None,
        title="User header",
        description="Header containing the authenticated username provided by the upstream proxy.",
    )
    role: str = Field(
        default=None,
        title="Role header",
        description="Header containing the authenticated user's role or groups from the upstream proxy.",
    )
    role_map: dict[str, list[str]] | None = Field(
        default_factory=dict,
        title=("Role mapping"),
        description="Map upstream group values to Frigate roles (for example map admin groups to the admin role).",
    )


class ProxyJwtConfig(FrigateBaseModel):
    issuer: str = Field(min_length=1, title="Trusted token issuer")
    audience: str = Field(min_length=1, title="Application client ID")
    jwks_url: str = Field(title="HTTPS signing keys URL")

    @field_validator("issuer", "jwks_url")
    @classmethod
    def require_https_origin(cls, value):
        parsed = urlsplit(value)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.fragment
            or parsed.query
        ):
            raise ValueError("Proxy JWT endpoints must be explicit HTTPS URLs")
        return value


class ProxyConfig(FrigateBaseModel):
    jwt: ProxyJwtConfig | None = Field(
        default=None,
        title="Signed proxy identity",
        description="Validate X-Authentik-JWT with RS256 and pinned issuer, audience, and HTTPS signing keys. Native authentication remains enabled for integrations.",
    )
    header_map: HeaderMappingConfig = Field(
        default_factory=HeaderMappingConfig,
        title="Header mapping",
        description="Map incoming proxy headers to Frigate user and role fields for proxy-based auth.",
    )
    logout_url: str | None = Field(
        default=None,
        title="Logout URL",
        description="URL to redirect users to when logging out via the proxy.",
    )
    auth_secret: EnvString | None = Field(
        default=None,
        title="Proxy secret",
        description="Optional secret checked against the X-Proxy-Secret header to verify trusted proxies.",
    )
    default_role: str | None = Field(
        default="viewer",
        title="Default role",
        description="Default role assigned to proxy-authenticated users when no role mapping applies.",
    )
    separator: str | None = Field(
        default=",",
        title="Separator character",
        description="Character used to split multiple values provided in proxy headers.",
    )

    @field_validator("separator", mode="before")
    @classmethod
    def validate_separator_length(cls, v):
        if v is not None and len(v) != 1:
            raise ValueError("Separator must be exactly one character")
        return v
