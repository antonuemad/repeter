"""Google OAuth 2.0 (authorization code + PKCE S256) for YouTube channels and Google Drive.

One provider serves two purposes: "youtube" (upload + read-only channel access) and "gdrive"
(read-only Drive access for the Drive folder source). Endpoints are from Google's OpenID
discovery document: https://accounts.google.com/.well-known/openid-configuration

Each purpose asks only for its own scopes (no include_granted_scopes), so a YouTube grant never
carries Drive access and the other way round. Both kinds of account store `extra["google_sub"]`,
the Google account id. Google revokes everything a project was granted by a user when any one
token is revoked (https://developers.google.com/identity/protocols/oauth2/web-server#tokenrevoke),
so the dashboard compares this id, not refresh-token strings, to decide whether disconnecting an
account may revoke at Google.
"""

from __future__ import annotations

import base64
import binascii
import dataclasses
import json
import logging
from typing import Any
from urllib.parse import quote, urlencode

import httpx

from repeater.config import get_settings
from repeater.models import Platform
from repeater.platforms import http, youtube
from repeater.platforms.base import (
    AccountContext,
    AuthError,
    ConnectedAccountData,
    OAuthTokens,
    PermanentError,
    RetryableError,
)

log = logging.getLogger(__name__)

AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"  # noqa: S105 - endpoint URL, not a secret
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"

YOUTUBE_UPLOAD = "https://www.googleapis.com/auth/youtube.upload"
YOUTUBE_READONLY = "https://www.googleapis.com/auth/youtube.readonly"
DRIVE_READONLY = "https://www.googleapis.com/auth/drive.readonly"

# "openid" is non-sensitive; it makes the token response carry an id_token whose `sub` identifies
# the Google account (see `resolve_google_sub`).
PURPOSE_SCOPES: dict[str, tuple[str, ...]] = {
    "youtube": ("openid", YOUTUBE_UPLOAD, YOUTUBE_READONLY),
    "gdrive": ("openid", "email", DRIVE_READONLY),
}
ID_TOKEN_ISSUERS = ("https://accounts.google.com", "accounts.google.com")
# Google's granular consent lets the user untick individual scopes, so check what was granted.
# openid/email come back as userinfo.* URLs and are not needed to work, so they are not required.
REQUIRED_SCOPES: dict[str, tuple[str, ...]] = {
    "youtube": (YOUTUBE_UPLOAD, YOUTUBE_READONLY),
    "gdrive": (DRIVE_READONLY,),
}
MISSING_SCOPE_MESSAGES = {
    "youtube": (
        "Google did not grant every permission Repeater needs for YouTube. Connect again and tick "
        "both boxes: upload videos, and view your YouTube account."
    ),
    "gdrive": (
        "Google did not grant read access to Google Drive. Connect again and tick the box that lets "
        "Repeater see your Drive files."
    ),
}
# Google always sends expires_in; if it ever does not, refresh after the usual hour.
DEFAULT_ACCESS_TOKEN_SECONDS = 3600

REFRESH_INVALID_GRANT = (
    "Google no longer accepts this account's sign-in (access was removed, the password changed, "
    "or the sign-in expired). Reconnect the account. Note: while the Google Cloud app is in "
    "'Testing' publishing status, Google expires refresh tokens after 7 days."
)
EXCHANGE_INVALID_GRANT = "The Google sign-in expired or was already used. Start the connection again."
NO_OFFLINE_ACCESS = (
    "Google did not return offline access for this account. Remove Repeater at "
    "https://myaccount.google.com/permissions and connect again."
)


def _scopes_for(purpose: str) -> tuple[str, ...]:
    try:
        return PURPOSE_SCOPES[purpose]
    except KeyError:
        raise ValueError(f"unknown Google OAuth purpose {purpose!r}") from None


def _client_credentials() -> tuple[str, str]:
    s = get_settings()
    if not (s.google_client_id and s.google_client_secret):
        raise PermanentError(
            "Google sign-in is not configured on this server "
            "(set REPEATER_GOOGLE_CLIENT_ID and REPEATER_GOOGLE_CLIENT_SECRET).",
            code="not_configured",
        )
    return s.google_client_id, s.google_client_secret


def _oauth_error(resp: httpx.Response) -> tuple[str | None, str]:
    return youtube.parse_api_error(resp)


def _token_request(c: httpx.Client, data: dict[str, str], *, invalid_grant: str) -> dict[str, Any]:
    """POST to the token endpoint and return the JSON body, mapping OAuth errors."""
    try:
        resp = c.post(TOKEN_URL, data=data, headers={"Accept": "application/json"})
    except httpx.TransportError as exc:
        raise RetryableError(f"Google sign-in: network error ({type(exc).__name__})") from exc
    if resp.status_code == 200:
        try:
            body = resp.json()
        except ValueError as exc:
            raise RetryableError("Google sign-in returned an unreadable response") from exc
        if not isinstance(body, dict):
            raise RetryableError("Google sign-in returned an unexpected response")
        return body
    error, description = _oauth_error(resp)
    status = resp.status_code
    if error == "invalid_grant":
        raise AuthError(invalid_grant, code=error, status=status)
    if error in ("invalid_client", "unauthorized_client"):
        raise PermanentError(
            "Google rejected Repeater's OAuth client. Check REPEATER_GOOGLE_CLIENT_ID and "
            "REPEATER_GOOGLE_CLIENT_SECRET and the client's redirect URIs.",
            code=error,
            status=status,
        )
    detail = f": {error}" + (f" ({description})" if description else "") if error else ""
    raise http.classify_http_error(resp, f"Google token request failed (HTTP {status}){detail}", code=error)


def tokens_from_response(
    payload: dict[str, Any],
    *,
    previous: OAuthTokens | None = None,
    fallback_scopes: tuple[str, ...] = (),
) -> OAuthTokens:
    """Build OAuthTokens from a token-endpoint response. On refresh Google usually omits
    refresh_token and sometimes scope; the previous values are kept in that case."""
    access_token = payload.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise RetryableError("Google sign-in returned no access token")
    new_refresh = payload.get("refresh_token") if isinstance(payload.get("refresh_token"), str) else None
    refresh_token = new_refresh or (previous.refresh_token if previous else None)
    if payload.get("refresh_token_expires_in"):
        refresh_expires_at = OAuthTokens.expiry_from_seconds(payload["refresh_token_expires_in"])
    elif previous is not None and not new_refresh:
        refresh_expires_at = previous.refresh_expires_at
    else:
        refresh_expires_at = None
    scope = payload.get("scope")
    if isinstance(scope, str) and scope.strip():
        scopes = scope.split()
    else:
        scopes = list(previous.scopes) if previous and previous.scopes else list(fallback_scopes)
    return OAuthTokens(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_at=OAuthTokens.expiry_from_seconds(payload.get("expires_in") or DEFAULT_ACCESS_TOKEN_SECONDS),
        refresh_expires_at=refresh_expires_at,
        scopes=scopes,
        token_type=payload.get("token_type") or "Bearer",
        extra=dict(previous.extra) if previous else {},
    )


def _require_scopes(purpose: str, granted: list[str]) -> None:
    missing = [s for s in REQUIRED_SCOPES[purpose] if s not in granted]
    if missing:
        raise PermanentError(MISSING_SCOPE_MESSAGES[purpose], code="scope_not_granted")


def _copy_tokens(tokens: OAuthTokens) -> OAuthTokens:
    return dataclasses.replace(tokens, scopes=list(tokens.scopes), extra=dict(tokens.extra))


def _channel_account(
    channel: dict[str, Any], tokens: OAuthTokens, google_sub: str | None = None
) -> ConnectedAccountData:
    info = youtube.channel_summary(channel)
    extra = dict(info["extra"])
    if google_sub:
        extra["google_sub"] = google_sub
    return ConnectedAccountData(
        platform=Platform.YOUTUBE,
        external_id=str(channel["id"]),
        display_name=info["display_name"],
        tokens=_copy_tokens(tokens),
        avatar_url=info["avatar_url"],
        extra=extra,
    )


def _youtube_accounts(
    c: httpx.Client, tokens: OAuthTokens, google_sub: str | None = None
) -> list[ConnectedAccountData]:
    channels = youtube.list_my_channels(c, tokens.access_token)
    if not channels:
        raise PermanentError(youtube.NO_CHANNEL_MESSAGE, code="no_channel")
    return [_channel_account(ch, tokens, google_sub) for ch in channels]


def _id_token_sub(id_token: Any, client_id: str) -> str | None:
    """`sub` of the id_token Google returned from the token endpoint. The token came straight from
    Google over TLS, so (OpenID Connect Core 3.1.3.7) its signature need not be checked; issuer and
    audience still are."""
    if not isinstance(id_token, str):
        return None
    parts = id_token.split(".")
    if len(parts) != 3:
        return None
    try:
        claims = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
    except (binascii.Error, ValueError):
        return None
    if not isinstance(claims, dict) or claims.get("iss") not in ID_TOKEN_ISSUERS:
        return None
    audience = claims.get("aud")
    if audience != client_id and not (isinstance(audience, list) and client_id in audience):
        return None
    sub = claims.get("sub")
    return str(sub) if sub else None


def _userinfo_sub(c: httpx.Client, access_token: str) -> str | None:
    try:
        resp = c.get(USERINFO_URL, headers={"Authorization": f"Bearer {access_token}"})
        info = resp.json() if resp.status_code == 200 else None
    except (httpx.TransportError, ValueError):
        return None
    sub = info.get("sub") if isinstance(info, dict) else None
    return str(sub) if sub else None


def resolve_google_sub(
    c: httpx.Client, payload: dict[str, Any], client_id: str, access_token: str
) -> str | None:
    """The Google account id of a fresh authorization: from the id_token, else from userinfo.
    Best effort: an account without it is still connected (the dashboard then treats it as an
    older account whose sign-in identity is unknown)."""
    sub = _id_token_sub(payload.get("id_token"), client_id) or _userinfo_sub(c, access_token)
    if sub is None:
        log.info("Google sign-in returned no account id (sub); revoke checks fall back to a cautious rule")
    return sub


def _drive_account(c: httpx.Client, tokens: OAuthTokens) -> ConnectedAccountData:
    try:
        resp = c.get(USERINFO_URL, headers={"Authorization": f"Bearer {tokens.access_token}"})
    except httpx.TransportError as exc:
        raise RetryableError(f"Google profile lookup: network error ({type(exc).__name__})") from exc
    if resp.status_code != 200:
        error, _ = _oauth_error(resp)
        raise http.classify_http_error(
            resp, f"Google profile lookup failed (HTTP {resp.status_code})", code=error
        )
    try:
        info = resp.json()
    except ValueError as exc:
        raise RetryableError("Google profile lookup returned an unreadable response") from exc
    sub = info.get("sub") if isinstance(info, dict) else None
    if not sub:
        raise PermanentError("Google did not return an account id for this sign-in.", code="no_sub")
    email = info.get("email") or ""
    return ConnectedAccountData(
        platform=Platform.GDRIVE,
        external_id=str(sub),
        display_name=email or str(sub),
        tokens=tokens,
        avatar_url=info.get("picture"),
        extra={"email": email, "google_sub": str(sub)},
    )


class GoogleOAuth:
    name = "google"
    uses_pkce = True

    def authorize_url(
        self, *, state: str, redirect_uri: str, code_challenge: str | None, purpose: str
    ) -> str:
        scopes = _scopes_for(purpose)
        if not code_challenge:
            raise ValueError("Google sign-in requires a PKCE code_challenge")
        client_id, _ = _client_credentials()
        params = {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": " ".join(scopes),
            "access_type": "offline",
            "prompt": "consent",
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
        return f"{AUTHORIZE_URL}?{urlencode(params, quote_via=quote)}"

    def exchange_code(
        self, *, code: str, redirect_uri: str, code_verifier: str | None, purpose: str
    ) -> list[ConnectedAccountData]:
        scopes = _scopes_for(purpose)
        client_id, client_secret = _client_credentials()
        data = {
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        }
        if code_verifier:
            data["code_verifier"] = code_verifier
        with http.client() as c:
            payload = _token_request(c, data, invalid_grant=EXCHANGE_INVALID_GRANT)
            tokens = tokens_from_response(payload, fallback_scopes=scopes)
            _require_scopes(purpose, tokens.scopes)
            if not tokens.refresh_token:
                raise PermanentError(NO_OFFLINE_ACCESS, code="no_refresh_token")
            if purpose == "youtube":
                sub = resolve_google_sub(c, payload, client_id, tokens.access_token)
                return _youtube_accounts(c, tokens, sub)
            return [_drive_account(c, tokens)]

    def refresh(self, account: AccountContext) -> OAuthTokens:
        refresh_token = account.tokens.refresh_token
        if not refresh_token:
            raise AuthError(
                "No Google refresh token is stored for this account. Reconnect it.", code="no_refresh_token"
            )
        client_id, client_secret = _client_credentials()
        data = {
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        }
        with http.client() as c:
            payload = _token_request(c, data, invalid_grant=REFRESH_INVALID_GRANT)
        return tokens_from_response(payload, previous=account.tokens)

    def revoke(self, account: AccountContext) -> None:
        """Revoke the grant. Revoking the refresh token also invalidates its access tokens."""
        token = account.tokens.refresh_token or account.tokens.access_token
        if not token:
            return
        with http.client() as c:
            try:
                # Token in the form body rather than the query string, so it never lands in URL logs.
                resp = c.post(REVOKE_URL, data={"token": token})
            except httpx.TransportError as exc:
                raise RetryableError(
                    f"Google token revocation: network error ({type(exc).__name__})"
                ) from exc
        if resp.status_code == 200:
            return
        error, _ = _oauth_error(resp)
        if resp.status_code == 400 and error == "invalid_token":
            return  # already revoked or expired: nothing left to revoke
        raise http.classify_http_error(
            resp, f"Google token revocation failed (HTTP {resp.status_code})", code=error
        )


oauth = GoogleOAuth()
