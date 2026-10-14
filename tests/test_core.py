"""Tests for the core: crypto, queue, accounts, storage."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from repeater import crypto, queue
from repeater.accounts import (
    AccountUnavailable,
    disconnect,
    fresh_context,
    load_tokens,
    upsert_connected_accounts,
)
from repeater.models import (
    AccountStatus,
    ConnectedAccount,
    Job,
    JobStatus,
    Platform,
    PostStatus,
    RotationRule,
    ScheduledPost,
    Video,
    VideoStatus,
)
from repeater.platforms import override
from repeater.platforms.base import AuthError, ConnectedAccountData, OAuthTokens
from repeater.timeutil import utcnow


def test_encrypt_roundtrip_and_tamper():
    token = crypto.encrypt_json({"access_token": "secret-value", "n": 1})
    assert "secret-value" not in token
    assert crypto.decrypt_json(token) == {"access_token": "secret-value", "n": 1}
    env = __import__("json").loads(token)
    env["c"] = env["c"][:-2] + ("AA" if env["c"][-2:] != "AA" else "BB")
    with pytest.raises(crypto.CryptoError):
        crypto.decrypt_json(__import__("json").dumps(env))


def test_key_rotation(monkeypatch):
    from repeater.config import get_settings

    old_spec = get_settings().token_encryption_keys
    sealed = crypto.encrypt_json({"a": 1})
    new_spec = crypto.generate_key_spec("t2") + "," + old_spec
    monkeypatch.setattr(get_settings(), "token_encryption_keys", new_spec)
    assert crypto.decrypt_json(sealed) == {"a": 1}
    assert crypto.needs_rewrap(sealed)
    assert not crypto.needs_rewrap(crypto.encrypt_json({"a": 1}))


def test_signed_media_url():
    path = crypto.signed_media_path(42, ttl_seconds=60, now=1_000_000)
    _, m, vid, exp, sig = path.split("/")
    assert m == "m" and vid == "42"
    sig = sig.removesuffix(".mp4")
    assert crypto.verify_media_signature(42, int(exp), sig, now=1_000_000) == "ok"
    assert crypto.verify_media_signature(43, int(exp), sig, now=1_000_000) == "invalid"
    assert crypto.verify_media_signature(42, int(exp), sig, now=1_000_100) == "expired"
    assert crypto.signed_media_url(1).startswith("https://rotator.test/m/1/")


def test_queue_dedupe_claim_and_retry(db):
    a = queue.enqueue(db, "k", {"x": 1}, dedupe_key="k:1")
    b = queue.enqueue(db, "k", {"x": 1}, dedupe_key="k:1")
    assert a is not None and b is None
    db.commit()
    job = queue.claim_next(db, "w1")
    assert job is not None and job.status == JobStatus.RUNNING and job.attempts == 1
    db.commit()
    # still active → dedupe blocks a second one
    assert queue.enqueue(db, "k", {}, dedupe_key="k:1") is None
    assert queue.fail(db, job, "boom", retry_in=timedelta(seconds=0)) is True
    db.commit()
    job2 = queue.claim_next(db, "w1")
    assert job2.id == job.id and job2.attempts == 2
    queue.complete(db, job2)
    db.commit()
    # done → a new job with the same key is allowed
    assert queue.enqueue(db, "k", {}, dedupe_key="k:1") is not None


def test_queue_skip_locked(db):
    from repeater.db import get_sessionmaker

    queue.enqueue(db, "k", {})
    queue.enqueue(db, "k", {})
    db.commit()
    s1, s2 = get_sessionmaker()(), get_sessionmaker()()
    try:
        j1 = queue.claim_next(s1, "a")
        j2 = queue.claim_next(s2, "b")
        assert j1 and j2 and j1.id != j2.id
        assert queue.claim_next(s2, "b") is None
    finally:
        s1.rollback(), s2.rollback(), s1.close(), s2.close()


def test_queue_reschedule_and_dead(db):
    queue.enqueue(db, "k", {}, max_attempts=1)
    db.commit()
    job = queue.claim_next(db, "w")
    queue.reschedule(db, job, 0, "not yet")
    db.commit()
    job = queue.claim_next(db, "w")
    assert job.attempts == 1
    assert queue.fail(db, job, "fatal", retry_in=timedelta(seconds=1)) is False
    db.commit()
    assert db.get(Job, job.id).status == JobStatus.DEAD


def test_queue_requeue_stale(db):
    queue.enqueue(db, "k", {})
    db.commit()
    job = queue.claim_next(db, "w")
    job.locked_at = utcnow() - timedelta(hours=2)
    db.commit()
    assert queue.requeue_stale(db, 30) == 1


def test_upsert_and_fresh_context_refresh(db, workspace):
    accs = upsert_connected_accounts(
        db,
        workspace.id,
        [
            ConnectedAccountData(
                platform=Platform.TIKTOK,
                external_id="open-1",
                display_name="Creator",
                tokens=OAuthTokens(
                    access_token="old", refresh_token="r1", expires_at=utcnow() + timedelta(minutes=5)
                ),
            )
        ],
    )
    db.commit()
    acc = accs[0]
    assert load_tokens(acc).access_token == "old"

    class FakeOAuth:
        name, uses_pkce = "tiktok", False

        def refresh(self, ctx):
            assert ctx.tokens.refresh_token == "r1"
            return OAuthTokens(
                access_token="new", refresh_token="r2", expires_at=utcnow() + timedelta(hours=24)
            )

    with override("oauth", "tiktok", FakeOAuth()):
        ctx = fresh_context(db, acc)
    assert ctx.tokens.access_token == "new"
    assert load_tokens(acc).refresh_token == "r2"


def test_fresh_context_auth_error_marks_reconnect(db, make_account):
    acc = make_account("tiktok", expires_in=timedelta(minutes=1))

    class BadOAuth:
        name, uses_pkce = "tiktok", False

        def refresh(self, ctx):
            raise AuthError("refresh_token is invalid")

    with override("oauth", "tiktok", BadOAuth()), pytest.raises(AccountUnavailable):
        fresh_context(db, acc)
    assert acc.status == AccountStatus.NEEDS_RECONNECT


def test_disconnect_cancels_work(db, workspace, make_account):
    acc = make_account("instagram")
    v = Video(workspace_id=workspace.id, status=VideoStatus.READY)
    db.add(v)
    db.flush()
    db.add(RotationRule(workspace_id=workspace.id, connected_account_id=acc.id, platform=Platform.INSTAGRAM))
    db.add(
        ScheduledPost(
            workspace_id=workspace.id,
            video_id=v.id,
            connected_account_id=acc.id,
            platform=Platform.INSTAGRAM,
            idempotency_key="k",
            scheduled_at=utcnow(),
        )
    )
    db.commit()

    class FakeMeta:
        name, uses_pkce, revoked = "meta", False, False

        def revoke(self, ctx):
            FakeMeta.revoked = True

    with override("oauth", "meta", FakeMeta()):
        disconnect(db, acc)
    db.commit()
    assert FakeMeta.revoked
    acc = db.get(ConnectedAccount, acc.id)
    assert acc.status == AccountStatus.DISABLED and acc.token_enc is None
    post = db.execute(select(ScheduledPost)).scalar_one()
    assert post.status == PostStatus.CANCELED
    assert db.execute(select(RotationRule)).scalar_one().enabled is False


def test_storage_paths():
    from repeater.media import storage

    d = storage.ensure_video_dir(3, 9)
    assert d.is_dir() and storage.rel_path(d / "x.mp4") == "ws3/v9/x.mp4"
    assert storage.abs_path("ws3/v9/x.mp4") == d / "x.mp4"
    with pytest.raises(ValueError):
        storage.abs_path("../../etc/passwd")


# =========================================================================== queue: priority, leases


def test_claim_order_priority_class_before_run_at(db):
    """engine-7: a backfill of imports queued earlier never delays a due publish or poll."""
    t0 = utcnow() - timedelta(hours=3)
    for vid in range(1, 201):
        queue.enqueue(db, "import_video", {"video_id": vid}, run_at=t0, dedupe_key=f"import_video:{vid}")
        queue.enqueue(db, "process_video", {"video_id": vid}, run_at=t0, dedupe_key=f"process_video:{vid}")
    queue.enqueue(db, "poll_source", {"source_id": 1}, run_at=t0)
    queue.enqueue(db, "plan_workspace", {"workspace_id": 1}, run_at=t0 + timedelta(hours=1))
    queue.enqueue(
        db, "publish_post", {"post_id": 9}, run_at=t0 + timedelta(minutes=10), dedupe_key="publish_post:9"
    )
    queue.enqueue(db, "poll_post", {"post_id": 8}, run_at=t0 + timedelta(hours=1), dedupe_key="poll_post:8")
    queue.enqueue(db, "publish_post", {"post_id": 10}, run_at=utcnow() + timedelta(hours=1))  # not due
    db.commit()
    order = []
    while (job := queue.claim_next(db, "w")) is not None:
        order.append(job.kind)
        queue.complete(db, job)
        db.commit()
    assert order[:2] == ["publish_post", "poll_post"]  # urgent first, then by run_at inside the class
    assert order[2] == "plan_workspace" and order[3] == "poll_source"
    assert set(order[4:]) == {"import_video", "process_video"} and len(order) == 404


def test_claim_next_with_kinds_filter_keeps_priority(db):
    queue.enqueue(db, "import_video", {}, run_at=utcnow() - timedelta(hours=1))
    queue.enqueue(db, "publish_post", {}, run_at=utcnow() - timedelta(minutes=1))
    db.commit()
    assert queue.claim_next(db, "w", ["import_video"]).kind == "import_video"
    assert queue.claim_next(db, "w", ["import_video"]) is None


def test_lease_renewal_and_stale_requeue_only_for_lapsed_leases(db):
    """deploy-ops-6/engine-2: a job whose worker renews its lease is never requeued, however long
    it runs; one whose lease lapsed (its worker died) is."""
    queue.enqueue(db, "k", {})
    queue.enqueue(db, "k", {})
    db.commit()
    alive, dead = queue.claim_next(db, "w:0"), queue.claim_next(db, "w:1")
    db.commit()
    long_ago = utcnow() - timedelta(hours=3)
    alive.locked_at = dead.locked_at = long_ago
    db.commit()
    assert queue.renew_lease(db, alive.id, "w:0") is True
    assert queue.renew_lease(db, alive.id, "someone-else") is False
    db.commit()
    assert queue.requeue_stale(db, 5) == 1
    db.commit()
    db.expire_all()
    assert db.get(Job, alive.id).status == JobStatus.RUNNING
    assert db.get(Job, dead.id).status == JobStatus.QUEUED and db.get(Job, dead.id).locked_by is None
    assert queue.renew_lease(db, dead.id, "w:1") is False  # no longer held


def test_lease_timeout_is_never_shorter_than_a_few_renewals(monkeypatch):
    from repeater.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "job_lock_timeout_minutes", 1)
    monkeypatch.setattr(settings, "job_lease_refresh_seconds", 120)
    assert queue.lease_timeout_minutes() == 6.0
    monkeypatch.setattr(settings, "job_lock_timeout_minutes", 30)
    assert queue.lease_timeout_minutes() == 30.0


def test_release_owned_returns_this_workers_jobs_without_consuming_attempts(db):
    for _ in range(3):
        queue.enqueue(db, "k", {})
    db.commit()
    mine1, mine2, theirs = (queue.claim_next(db, w) for w in ("host:1:0", "host:1:1", "host:2:0"))
    db.commit()
    assert queue.release_owned(db, "host:1:") == 2
    db.commit()
    db.expire_all()
    for job in (mine1, mine2):
        row = db.get(Job, job.id)
        assert row.status == JobStatus.QUEUED and row.attempts == 0 and row.locked_by is None
    assert db.get(Job, theirs.id).status == JobStatus.RUNNING
    # `host:1%` must not match `host:12:0` (LIKE wildcards are escaped).
    queue.enqueue(db, "k", {})
    db.commit()
    other = queue.claim_next(db, "host:12:0")
    db.commit()
    assert queue.release_owned(db, "host:1:") == 0 and db.get(Job, other.id).status == JobStatus.RUNNING


def test_enqueue_or_move_moves_queued_and_adds_a_follow_up_behind_a_running_job(db):
    now = utcnow()
    a = queue.enqueue_or_move(db, "k", {"x": 1}, dedupe_key="k:1", run_at=now + timedelta(hours=1))
    db.commit()
    # Queued: moved (later or earlier), no second job.
    b = queue.enqueue_or_move(db, "k", {"x": 1}, dedupe_key="k:1", run_at=now + timedelta(hours=2))
    assert b == a and db.get(Job, a).run_at == now + timedelta(hours=2)
    queue.enqueue_or_move(
        db, "k", {"x": 1}, dedupe_key="k:1", run_at=now + timedelta(hours=3), only_earlier=True
    )
    assert db.get(Job, a).run_at == now + timedelta(hours=2)  # only_earlier never delays
    queue.enqueue_or_move(db, "k", {"x": 1}, dedupe_key="k:1", run_at=now, only_earlier=True)
    assert db.get(Job, a).run_at == now
    db.commit()
    # Running: the caller's change would be missed, so a second job is queued under ":next".
    job = queue.claim_next(db, "w")
    db.commit()
    c = queue.enqueue_or_move(db, "k", {"x": 1}, dedupe_key="k:1", run_at=now)
    assert c not in (None, job.id)
    d = queue.enqueue_or_move(db, "k", {"x": 1}, dedupe_key="k:1", run_at=now + timedelta(minutes=1))
    db.commit()
    assert d == c  # the follow-up itself is moved, not duplicated
    keys = sorted(j.dedupe_key for j in db.execute(select(Job)).scalars())
    assert keys == ["k:1", "k:1:next"]


def test_enqueue_plan_workspace_without_run_at_asks_for_a_plan_soon(db):
    from repeater.jobs import kinds

    boundary = utcnow() + timedelta(minutes=4)
    assert kinds.enqueue_plan_workspace(db, 5, run_at=boundary) is not None  # the periodic tick
    assert kinds.enqueue_plan_workspace(db, 5, run_at=boundary) is None  # already queued for later
    job_id = kinds.enqueue_plan_workspace(db, 5)  # a user change: moved up to now
    db.commit()
    assert db.get(Job, job_id).run_at <= utcnow()
    job = queue.claim_next(db, "w")
    db.commit()
    # A plan that is running started before the change: a second one is queued behind it.
    follow_up = kinds.enqueue_plan_workspace(db, 5)
    db.commit()
    assert follow_up not in (None, job.id) and db.get(Job, follow_up).dedupe_key == "plan_workspace:5:next"
