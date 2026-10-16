"""Sign-in, sign-out, login rate limiting and the user/workspace dependencies."""

from __future__ import annotations

import hashlib
import hmac
import threading
import time
from collections import deque
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from repeater.activity import log_activity
from repeater.config import get_settings
from repeater.models import ActivityLevel, User, Workspace, WorkspaceMember
from repeater.security import hash_password, needs_rehash, verify_password
from repeater.timeutil import utcnow
from repeater.web import consent, csrf, i18n
from repeater.web.deps import DB, Form, FormError, form_str, safe_next
from repeater.web.templating import render

USER_KEY = "uid"
FINGERPRINT_KEY = "pwv"
ROLES = ("owner", "editor")
MAX_LOGIN_FAILURES = 10
LOGIN_WINDOW_SECONDS = 15 * 60
MAX_PASSWORD_CHARS = 1024
# argon2id with the library defaults needs 64 MiB per verification. The dashboard container has
# 1 GiB and half a CPU, so at most this many hashes run at the same time (anything beyond waits at
# most HASH_WAIT_SECONDS and is then answered with 503). Without this bound a burst of logins
# allocates one 64 MiB hash per worker thread and the container is OOM-killed.
HASH_CONCURRENCY = 3
HASH_WAIT_SECONDS = 1.0
_hash_slots = threading.BoundedSemaphore(HASH_CONCURRENCY)
# Verified when the email is unknown so both paths cost one argon2 verification.
_DUMMY_HASH = hash_password("repeater-timing-equaliser")


class LoginRequired(Exception):  # noqa: N818 - control flow, handled by a redirect to /login
    pass


class ConsentRequired(Exception):  # noqa: N818 - control flow, handled by a redirect to /consent
    """The signed-in user has not accepted the current terms and privacy policy yet."""


class HashingBusy(Exception):  # noqa: N818 - control flow, answered with 503
    """All password-hashing slots stayed busy for HASH_WAIT_SECONDS."""


@contextmanager
def hash_slot() -> Iterator[None]:
    """Bounds concurrent argon2 work in this process (see HASH_CONCURRENCY)."""
    if not _hash_slots.acquire(timeout=HASH_WAIT_SECONDS):
        raise HashingBusy()
    try:
        yield
    finally:
        _hash_slots.release()


# --------------------------------------------------------------------------- rate limiting


class LoginRateLimiter:
    """Sliding-window counter of failed sign-ins per key (IP address and email), in memory.
    A single web process serves the dashboard, so this does not need shared storage."""

    def __init__(
        self,
        max_failures: int = MAX_LOGIN_FAILURES,
        window_seconds: float = LOGIN_WINDOW_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        max_keys: int = 10_000,
    ) -> None:
        self.max_failures = max_failures
        self.window = window_seconds
        self.clock = clock
        self.max_keys = max_keys
        self._failures: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def _recent(self, key: str, now: float) -> deque[float]:
        entries = self._failures.get(key)
        if entries is None:
            return deque()
        while entries and entries[0] <= now - self.window:
            entries.popleft()
        if not entries:
            self._failures.pop(key, None)
        return entries

    def blocked(self, *keys: str) -> bool:
        now = self.clock()
        with self._lock:
            return any(len(self._recent(k, now)) >= self.max_failures for k in keys)

    def record_failure(self, *keys: str) -> None:
        now = self.clock()
        with self._lock:
            if len(self._failures) >= self.max_keys:
                self._prune(now)
            for key in keys:
                self._recent(key, now)
                self._failures.setdefault(key, deque()).append(now)

    def try_begin(self, *keys: str) -> bool:
        """Atomically check every key against the limit and, when none is at it, count one
        attempt on each of them right away. Counting before the (slow) password check is what
        stops a burst of concurrent requests from all passing the gate. Returns False, and counts
        nothing, when any key is at the limit."""
        now = self.clock()
        with self._lock:
            if any(len(self._recent(k, now)) >= self.max_failures for k in keys):
                return False
            if len(self._failures) >= self.max_keys:
                self._prune(now)
            for key in keys:
                self._failures.setdefault(key, deque()).append(now)
            return True

    def refund(self, *keys: str) -> None:
        """Take back one attempt counted by try_begin (a successful sign-in, or a request that
        never reached the password check)."""
        with self._lock:
            for key in keys:
                entries = self._failures.get(key)
                if entries:
                    entries.pop()
                if entries is not None and not entries:
                    self._failures.pop(key, None)

    def reset(self, key: str) -> None:
        with self._lock:
            self._failures.pop(key, None)

    def _prune(self, now: float) -> None:
        for key in list(self._failures):
            self._recent(key, now)
        while len(self._failures) >= self.max_keys:
            self._failures.pop(next(iter(self._failures)))


def client_ip(request: Request) -> str:
    # Behind Caddy, uvicorn must run with --proxy-headers and --forwarded-allow-ips so that
    # request.client is the real visitor address.
    return request.client.host if request.client else "unknown"


# --------------------------------------------------------------------------- session helpers


def password_fingerprint(user: User) -> str:
    """Ties a session to the current password hash: changing the password ends old sessions."""
    key = get_settings().secret_key.encode()
    return hmac.new(
        key, f"{user.id}:{user.password_hash}:{user.session_version}".encode(), hashlib.sha256
    ).hexdigest()[:32]


def start_session(request: Request, user: User) -> None:
    """Session fixation protection: drop everything from the anonymous session (including the
    CSRF token) and start a new one for the user."""
    request.session.clear()
    request.session[USER_KEY] = user.id
    request.session[FINGERPRINT_KEY] = password_fingerprint(user)
    csrf.rotate_token(request.session)


# --------------------------------------------------------------------------- dependencies


def session_user(request: Request, db: Session) -> User | None:
    """The signed-in user, or None when the session is missing, stale or the user is inactive."""
    uid = request.session.get(USER_KEY)
    if not isinstance(uid, int):
        return None
    user = db.get(User, uid)
    fingerprint = request.session.get(FINGERPRINT_KEY)
    if (
        user is None
        or not user.is_active
        or not isinstance(fingerprint, str)
        or not hmac.compare_digest(fingerprint, password_fingerprint(user))
    ):
        return None
    return user


@dataclass(frozen=True)
class UserView:
    """Plain snapshot used by page templates, so error pages can render after the database
    session of the request is closed."""

    id: int
    email: str
    locale: str
    is_platform_admin: bool = False


@dataclass(frozen=True)
class WorkspaceView:
    id: int
    name: str
    timezone: str
    paused: bool


def signed_in_user(request: Request, db: DB) -> User:
    """The signed-in user, whether or not the legal texts were accepted (the consent screen and
    sign-out work before that)."""
    user = session_user(request, db)
    if user is None:
        if USER_KEY in request.session:
            request.session.clear()
        raise LoginRequired()
    request.state.user = UserView(
        id=user.id, email=user.email, locale=user.locale, is_platform_admin=user.is_platform_admin
    )
    return user


def current_user(request: Request, db: DB) -> User:
    """The signed-in user who accepted the current terms and privacy policy. Everything but the
    consent screen itself needs this: nobody uses the client before agreeing (YouTube Developer
    Policies III.A.2)."""
    user = signed_in_user(request, db)
    if not consent.has_consented(request.session, db, user):
        raise ConsentRequired()
    return user


SignedInUser = Annotated[User, Depends(signed_in_user)]
CurrentUser = Annotated[User, Depends(current_user)]


@dataclass
class WorkspaceContext:
    user: User
    workspace: Workspace
    role: str

    @property
    def is_owner(self) -> bool:
        return self.role == "owner"

    @property
    def actor(self) -> str:
        return self.user.email[:64]


def current_workspace(request: Request, user: CurrentUser, db: DB) -> WorkspaceContext:
    """The user's first workspace membership (one workspace per user in this version)."""
    member = db.execute(
        select(WorkspaceMember)
        .where(WorkspaceMember.user_id == user.id, WorkspaceMember.role.in_(ROLES))
        .order_by(WorkspaceMember.workspace_id)
        .limit(1)
    ).scalar_one_or_none()
    if member is None:
        raise HTTPException(status_code=403, detail="no_workspace")
    ws = member.workspace
    if ws.closed_at:
        raise HTTPException(status_code=403, detail="workspace_closed")
    request.state.workspace = WorkspaceView(id=ws.id, name=ws.name, timezone=ws.timezone, paused=ws.paused)
    request.state.role = member.role
    return WorkspaceContext(user=user, workspace=ws, role=member.role)


CurrentWorkspace = Annotated[WorkspaceContext, Depends(current_workspace)]


def require_owner(ctx: WorkspaceContext) -> None:
    if not ctx.is_owner:
        raise HTTPException(status_code=403, detail="owner_only")


# --------------------------------------------------------------------------- routes

router = APIRouter()


def _limiter(request: Request) -> LoginRateLimiter:
    return request.app.state.login_limiter


@router.get("/login")
def login_page(request: Request, db: DB, next: str = "/") -> Response:  # noqa: A002 - query parameter name
    if session_user(request, db) is not None:
        return RedirectResponse(safe_next(next), status_code=303)
    return render(request, "login.html", {"next": safe_next(next), "email": "", "error": None})


@router.post("/login")
def login_submit(request: Request, db: DB, form: Form) -> Response:
    next_url = safe_next(form.get("next") if isinstance(form.get("next"), str) else None)
    try:
        email = form_str(form, "email", max_len=320).lower()
    except FormError:
        email = ""
    raw_password = form.get("password")
    password = raw_password if isinstance(raw_password, str) else ""

    def fail(error: str, status: int) -> Response:
        return render(
            request, "login.html", {"next": next_url, "email": email, "error": error}, status_code=status
        )

    # Nothing secret is compared for an empty or absurd input, so it never reaches the hasher.
    if not email or not password or len(password) > MAX_PASSWORD_CHARS:
        return fail("login.invalid", 400)
    ip_key, email_key = f"ip:{client_ip(request)}", f"email:{email}"
    limiter = _limiter(request)
    # Count the attempt before the slow password check, atomically with the limit check.
    if not limiter.try_begin(ip_key, email_key):
        return fail("login.rate_limited", 429)
    try:
        user_id = _authenticate(db, email, password)
    except HashingBusy:
        limiter.refund(ip_key, email_key)
        return fail("login.busy", 503)
    user = db.get(User, user_id) if user_id is not None else None
    if user is None or not user.is_active:
        return fail("login.invalid", 400)
    limiter.reset(email_key)
    limiter.refund(ip_key)
    _record_login(db, user, password)
    start_session(request, user)
    response = RedirectResponse(next_url, status_code=303)
    if user.locale in i18n.SUPPORTED and i18n.normalize(request.cookies.get(i18n.COOKIE)) is None:
        i18n.set_cookie(response, user.locale)
    return response


@router.post("/logout")
def logout(request: Request) -> Response:
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


def _authenticate(db: Session, email: str, password: str) -> int | None:
    """The user's id when the credentials are right. The database connection is returned to the
    pool before the (slow, 64 MiB) hash runs, and unknown or inactive users cost one verification
    as well so the answer does not depend on whether the email exists."""
    row = db.execute(
        select(User.id, User.password_hash, User.is_active).where(func.lower(User.email) == email)
    ).one_or_none()
    db.rollback()
    with hash_slot():
        if row is None or not row.is_active:
            verify_password(_DUMMY_HASH, password)
            return None
        return int(row.id) if verify_password(row.password_hash, password) else None


def _record_login(db: Session, user: User, password: str) -> None:
    user.last_login_at = utcnow()
    if needs_rehash(user.password_hash):
        try:
            with hash_slot():
                user.password_hash = hash_password(password)
        except HashingBusy:
            pass  # upgraded at the next sign-in
    workspace_id = db.execute(
        select(WorkspaceMember.workspace_id)
        .where(WorkspaceMember.user_id == user.id)
        .order_by(WorkspaceMember.workspace_id)
        .limit(1)
    ).scalar_one_or_none()
    log_activity(
        db,
        workspace_id,
        ActivityLevel.INFO,
        "auth",
        "Signed in to the dashboard",
        actor=user.email[:64],
        msg_key="activity.signed_in",
    )
    db.commit()
