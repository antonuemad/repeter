"""Regression coverage for the partial/low findings left in the handoff."""

from datetime import timedelta

import pytest
import respx
from sqlalchemy import select

from repeater.accounts import delete_account_records
from repeater.jobs import kinds, maintenance
from repeater.media import retention, storage
from repeater.models import ActivityLog, Job, JobStatus, Platform, RotationRule, Video, VideoStatus
from repeater.platforms import meta, tiktok
from repeater.platforms.base import AccountContext, OAuthTokens, RetryableError, UnknownOutcomeError
from repeater.timeutil import utcnow
from repeater.web.routes.overview import _incomplete_rules


@pytest.mark.parametrize("status", [500, 502, 503, 504])
@pytest.mark.parametrize("pull", [True, False])
def test_tiktok_pull_server_error_does_not_allow_a_second_init(status, pull):
    account = AccountContext(
        account_id=1,
        platform=Platform.TIKTOK,
        external_id="tt",
        display_name="Test",
        tokens=OAuthTokens(access_token="test"),
    )
    with respx.mock as router:
        init = router.post(tiktok.DIRECT_INIT_URL).respond(status, text="upstream gateway error")
        with pytest.raises(UnknownOutcomeError if pull else RetryableError):
            tiktok._start_upload(tiktok.DIRECT_INIT_URL, account, {}, "starting a video", pull=pull)
        assert init.call_count == 1


def test_deleting_an_account_preserves_other_accounts_activity(db, workspace, make_account):
    gone = make_account("instagram", display_name="magiclap")
    keep = make_account("tiktok", display_name="magiclap-official")
    own = ActivityLog(
        workspace_id=workspace.id,
        category="account",
        message="Disconnected magiclap",
        data={"account_id": gone.id},
    )
    other = ActivityLog(
        workspace_id=workspace.id,
        category="publish",
        message="Published to TikTok (magiclap-official)",
        data={"account_id": keep.id},
    )
    legacy = ActivityLog(
        workspace_id=workspace.id, category="system", message="magiclap was mentioned without ids", data={}
    )
    unrelated = ActivityLog(
        workspace_id=workspace.id,
        category="system",
        message="Published to TikTok (magiclap-official)",
        data={},
    )
    db.add_all([own, other, legacy, unrelated])
    db.commit()
    own_id, other_id, legacy_id, unrelated_id = own.id, other.id, legacy.id, unrelated.id
    delete_account_records(db, gone)
    db.commit()
    db.expire_all()
    assert db.get(ActivityLog, own_id) is None
    assert db.get(ActivityLog, other_id).message == "Published to TikTok (magiclap-official)"
    assert db.get(ActivityLog, unrelated_id).message == "Published to TikTok (magiclap-official)"
    assert "magiclap" not in db.get(ActivityLog, legacy_id).message


def test_archived_unencoded_original_expires_but_an_active_original_stays(db, workspace):
    rows = []
    for status in (VideoStatus.ARCHIVED, VideoStatus.PROCESSING):
        v = Video(workspace_id=workspace.id, status=status, created_at=utcnow() - timedelta(days=40))
        db.add(v)
        db.flush()
        p = storage.ensure_video_dir(workspace.id, v.id) / "original.mp4"
        p.write_bytes(b"original")
        v.original_path = storage.rel_path(p)
        rows.append((v, p))
    db.commit()
    assert retention.purge_expired_originals(db, older_than_days=30) == 1
    assert rows[0][0].original_path is None and not rows[0][1].exists()
    assert rows[1][0].original_path and rows[1][1].exists()


@pytest.mark.parametrize("with_dead_job", [True, False])
def test_abandoned_processing_original_releases_its_duplicates(db, workspace, with_dead_job):
    original = Video(
        workspace_id=workspace.id,
        status=VideoStatus.PROCESSING,
        title="Abandoned",
        updated_at=utcnow() - timedelta(hours=3),
    )
    db.add(original)
    db.flush()
    copy = Video(
        workspace_id=workspace.id, status=VideoStatus.DUPLICATE, title="Copy", duplicate_of_id=original.id
    )
    db.add(copy)
    db.flush()
    p = storage.ensure_video_dir(workspace.id, copy.id) / "original.mp4"
    p.write_bytes(b"copy")
    copy.original_path = storage.rel_path(p)
    if with_dead_job:
        db.add(Job(kind=kinds.PROCESS_VIDEO, payload={"video_id": original.id}, status=JobStatus.DEAD))
    db.commit()
    maintenance.run_maintenance(db)
    db.commit()
    db.expire_all()
    assert db.get(Video, original.id).status == VideoStatus.FAILED
    assert db.get(Video, copy.id).status == VideoStatus.PROCESSING
    assert db.get(Video, copy.id).duplicate_of_id is None
    assert db.execute(
        select(Job).where(Job.kind == kinds.PROCESS_VIDEO, Job.status == JobStatus.QUEUED)
    ).first()


def test_off_rules_do_not_create_overview_attention_warnings(db, workspace, make_account):
    account = make_account("tiktok")
    rule = RotationRule(
        workspace_id=workspace.id,
        connected_account_id=account.id,
        platform=Platform.TIKTOK,
        enabled=False,
        settings={},
    )
    db.add(rule)
    db.commit()
    assert _incomplete_rules(db, workspace.id) == []
    rule.enabled = True
    db.commit()
    assert _incomplete_rules(db, workspace.id)[0][0].id == rule.id


def test_meta_no_accounts_error_does_not_claim_optional_ads_scope_is_required():
    error = meta._no_accounts_error(frozenset({Platform.INSTAGRAM}), ["ads_read"], {})
    assert error.code == "no_publishable_accounts"
    assert "ads_read" not in error.message


@pytest.mark.parametrize(
    "addresses,allow,expected",
    [
        (["10.80.0.1"], "127.0.0.1,172.16.0.0/12", ["10.80.0.1"]),
        (["10.80.0.1", "10.80.0.5"], "127.0.0.1,10.80.0.0/24", []),
        (["::1", "fd00::5"], "::1,fd00::/64", []),
        (["0.0.0.0", "172.18.0.1"], "172.16.0.0/12", []),  # noqa: S104 - test input for a wildcard bind, not a listener
    ],
)
def test_real_proxy_addresses_are_checked_against_trust(addresses, allow, expected):
    from repeater.proxy import untrusted_proxy_addresses

    assert untrusted_proxy_addresses(addresses, allow) == expected
