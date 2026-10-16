"""Instagram Login for professional accounts, independent of Facebook Pages.

Uses Instagram app credentials (not the parent Meta app's credentials). Both login
methods share the Instagram publisher/lister; the persisted provider selects the host.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from urllib.parse import urlencode

import httpx

from repeater.config import get_settings
from repeater.models import Platform
from repeater.platforms import meta
from repeater.platforms.base import (
    AccountContext,
    AuthError,
    ConnectedAccountData,
    OAuthTokens,
    PermanentError,
    RetryableError,
)
from repeater.platforms.http import client
from repeater.timeutil import ensure_aware, utcnow

AUTHORIZE_URL = "https://www.instagram.com/oauth/authorize"
TOKEN_URL = "https://api.instagram.com/oauth/access_token"  # noqa: S105 - public endpoint
GRAPH_URL = "https://graph.instagram.com"
SCOPES = ("instagram_business_basic", "instagram_business_content_publish")


def credentials() -> tuple[str, str]:
    settings = get_settings()
    if not settings.platform_configured("instagram"):
        raise PermanentError("Instagram direct login is not configured.", code="not_configured")
    return settings.instagram_app_id, settings.instagram_app_secret


def identifier(value: object) -> str:
    value = str(value or "")
    if not re.fullmatch(r"[0-9]{1,64}", value):
        raise AuthError("Instagram did not return a valid account identity.", code="invalid_identity")
    return value


class Graph(meta.Graph):
    """Same fixed-host pagination/error handling, with an Instagram user token."""

    api_host = GRAPH_URL

    def proof(self, access_token: str) -> None:
        return None


def _token_request(http: httpx.Client, method: str, url: str, values: dict) -> dict:
    try:
        resp = http.request(method, url, **({"params": values} if method == "GET" else {"data": values}))
    except httpx.HTTPError as exc:
        raise RetryableError(f"Could not reach Instagram ({type(exc).__name__}).") from None
    # Never display the flat OAuth error_message, which can echo codes/secrets.
    payload = meta.json_or_none(resp)
    if isinstance(payload, dict) and payload.get("error_type"):
        raise AuthError("Instagram authorization was rejected. Connect again.", code="oauth_rejected")
    return meta.parse_response(resp, "connecting Instagram", tuple(str(v) for v in values.values()))


def _long_tokens(body: dict, scopes: list[str]) -> OAuthTokens:
    token = body.get("access_token")
    seconds = meta.as_int(body.get("expires_in"))
    if not isinstance(token, str) or not token or not seconds or not 0 < seconds <= 90 * 86400:
        raise AuthError("Instagram returned incomplete token data. Connect again.", code="invalid_token")
    now = utcnow()
    return OAuthTokens(
        access_token=token,
        expires_at=now + timedelta(seconds=seconds),
        scopes=scopes,
        extra={"issued_at": now.isoformat()},
    )


class InstagramOAuth:
    name = "instagram"
    uses_pkce = False

    def authorize_url(
        self, *, state: str, redirect_uri: str, code_challenge: str | None, purpose: str
    ) -> str:
        app_id, _ = credentials()
        return (
            AUTHORIZE_URL
            + "?"
            + urlencode(
                {
                    "client_id": app_id,
                    "redirect_uri": redirect_uri,
                    "response_type": "code",
                    "scope": ",".join(SCOPES),
                    "state": state,
                    "enable_fb_login": "false",
                    "force_reauth": "true",
                }
            )
        )

    def exchange_code(
        self, *, code: str, redirect_uri: str, code_verifier: str | None, purpose: str
    ) -> list[ConnectedAccountData]:
        app_id, secret = credentials()
        with client(timeout=meta.API_TIMEOUT) as http:
            response = _token_request(
                http,
                "POST",
                TOKEN_URL,
                {
                    "client_id": app_id,
                    "client_secret": secret,
                    "grant_type": "authorization_code",
                    "redirect_uri": redirect_uri,
                    "code": code,
                },
            )
            # Meta documents a data array; older responses use the same fields at top level.
            data = response.get("data", [response])
            if not isinstance(data, list) or len(data) != 1 or not isinstance(data[0], dict):
                raise AuthError("Instagram returned incomplete authorization data.")
            grant = data[0]
            login_id = identifier(grant.get("user_id"))
            short = grant.get("access_token")
            raw = grant.get("permissions")
            scopes = raw.replace(",", " ").split() if isinstance(raw, str) else raw
            if not isinstance(scopes, list) or not all(isinstance(s, str) for s in scopes):
                scopes = []
            if not set(SCOPES).issubset(scopes):
                raise AuthError(
                    "Allow profile, media and content permissions, then connect again.",
                    code="permissions_declined",
                )
            if not isinstance(short, str) or not short:
                raise AuthError("Instagram returned no access token.")
            tokens = _long_tokens(
                _token_request(
                    http,
                    "GET",
                    GRAPH_URL + "/access_token",
                    {
                        "grant_type": "ig_exchange_token",
                        "client_secret": secret,
                        "access_token": short,
                    },
                ),
                scopes,
            )
            with Graph(tokens.access_token, http=http) as graph:
                profile = graph.get(
                    "me",
                    {"fields": "id,user_id,username,name,account_type,profile_picture_url"},
                    action="reading Instagram account",
                )
            scoped_id = identifier(profile.get("id"))
            if scoped_id != login_id:
                raise AuthError("Instagram authorization identity did not match the profile.")
            ig_id = identifier(profile.get("user_id"))
            account_type = str(profile.get("account_type") or "").upper()
            if account_type not in {"BUSINESS", "MEDIA_CREATOR"}:
                raise AuthError("Use an Instagram Business or Creator account.", code="professional_required")
            username = str(profile.get("username") or "")
            return [
                ConnectedAccountData(
                    platform=Platform.INSTAGRAM,
                    external_id=ig_id,
                    display_name=username or str(profile.get("name") or f"Instagram {ig_id}"),
                    tokens=tokens,
                    avatar_url=profile.get("profile_picture_url"),
                    extra={
                        "oauth_provider": "instagram",
                        "ig_user_id": ig_id,
                        "ig_login_user_id": scoped_id,
                        "username": username,
                        "account_type": account_type,
                    },
                )
            ]

    def refresh(self, account: AccountContext) -> OAuthTokens:
        now = utcnow()
        if not account.tokens.expires_at or ensure_aware(account.tokens.expires_at) <= now:
            raise AuthError("Instagram access expired. Connect again.")
        try:
            issued = ensure_aware(datetime.fromisoformat(account.tokens.extra["issued_at"]))
        except (KeyError, TypeError, ValueError):
            raise AuthError("Instagram token age is unknown. Connect again.") from None
        if now - issued < timedelta(hours=24):
            return account.tokens  # Meta only refreshes tokens at least 24 hours old.
        with client(timeout=meta.API_TIMEOUT) as http:
            return _long_tokens(
                _token_request(
                    http,
                    "GET",
                    GRAPH_URL + "/refresh_access_token",
                    {
                        "grant_type": "ig_refresh_token",
                        "access_token": account.tokens.access_token,
                    },
                ),
                list(account.tokens.scopes),
            )

    def revoke(self, account: AccountContext) -> None:
        # A grant can serve several workspaces. Local disconnect must not revoke the
        # owner's other workspaces. UI explains how to remove Repeater in Instagram.
        return None


oauth = InstagramOAuth()
