"""Invited-customer acceptance, entirely synthetic with all external HTTP denied/mocked."""

import html
from datetime import timedelta
from urllib.parse import parse_qs, urlparse

import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from repeater.config import get_settings
from repeater.content_plans import simulate
from repeater.models import (
    AccountStatus,
    ConnectedAccount,
    ContentPlan,
    Job,
    Platform,
    RotationRule,
    ScheduledPost,
    Source,
    SourceKind,
    Video,
    VideoStatus,
    Workspace,
    WorkspaceInvitation,
)
from repeater.platforms import override
from repeater.platforms.base import ConnectedAccountData
from repeater.timeutil import utcnow
from tests.test_content_plans import FOLDER, create_plan, folder_mock, save, video
from tests.test_mixed_media import finish, raster, upload
from tests.test_web_access import NEW_PASSWORD, invite, issued_url, open_private, password_form
from tests.test_web_oauth import FakeProvider, _meta_accounts, _tokens
from tests.test_web_support import login, post, set_lang

pytest_plugins = ["tests.test_web_support"]


@pytest.fixture(autouse=True)
def no_external_http(monkeypatch):
    monkeypatch.setattr(get_settings(), "publishing_enabled", False)
    with respx.mock(assert_all_called=False) as mock:
        yield mock


def setup_card(client):
    return client.get("/").text.split('class="card setup"', 1)[1].split("</section>", 1)[0]


def connect(client, plan, purpose, fake):
    step = "content" if purpose == "gdrive" else "accounts"
    target = f"/plans/{plan.id}/setup/{step}"
    with override("oauth", fake.name, fake):
        response = client.get(f"/connect/{purpose}", params={"return_to": target}, follow_redirects=False)
        assert response.status_code == 303
        state = parse_qs(urlparse(response.headers["location"]).query)["state"][0]
        response = client.get(
            f"/oauth/{fake.name}/callback",
            params={"state": state, "code": "synthetic"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert response.headers["location"] == target


@pytest.mark.parametrize("lang", ["ar", "en"])
@pytest.mark.parametrize("instagram_method", ["meta", "instagram"])
def test_invitation_to_mixed_library_and_rotation(
    web_owner, web_app, db, admin_user, workspace, lang, no_external_http, instagram_method
):
    admin_user.is_platform_admin = True
    db.commit()
    url = issued_url(
        post(
            web_owner,
            "/admin/customers",
            {
                "name": "Synthetic invited customer",
                "email": "journey@example.com",
                "locale": lang,
            },
        )
    )
    invitation = db.scalar(select(WorkspaceInvitation))
    tenant = db.get(Workspace, invitation.workspace_id)
    assert tenant.id != workspace.id and tenant.paused
    with TestClient(web_app, base_url="https://rotator.test") as client:
        _, opened = open_private(client, url)
        assert opened.status_code == 303
        page = client.get("/invite/accept")
        assert f'<html lang="{lang}"' in page.text
        assert post(client, "/invite/accept", password_form(), page="/invite/accept").status_code == 303
        assert login(client, "journey@example.com", NEW_PASSWORD).status_code == 303
        assert 'href="/setup"' in setup_card(client)
        created = post(client, "/plans", {"name": "ليلى · Synthetic creator", "wizard_step": "accounts"})
        plan = db.get(ContentPlan, int(created.headers["location"].split("/")[2]))
        assert client.get(created.headers["location"]).status_code == 200
        assert plan.workspace_id == tenant.id

        meta_accounts = _meta_accounts()
        meta_accounts[1].extra["account_type"] = "BUSINESS"
        if instagram_method == "instagram":
            direct = meta_accounts.pop()
            direct.extra = {
                "oauth_provider": "instagram",
                "ig_login_user_id": "1234",
                "ig_user_id": direct.external_id,
                "account_type": "BUSINESS",
            }
            connect(client, plan, "instagram", FakeProvider("instagram", False, [direct]))
        connect(client, plan, "meta", FakeProvider("meta", False, meta_accounts))
        connect(
            client,
            plan,
            "gdrive",
            FakeProvider(
                "google",
                True,
                [
                    ConnectedAccountData(
                        platform=Platform.GDRIVE,
                        external_id="synthetic-drive",
                        display_name="Synthetic Drive",
                        tokens=_tokens(),
                    )
                ],
            ),
        )
        accounts = list(
            db.scalars(
                select(ConnectedAccount).where(
                    ConnectedAccount.workspace_id == tenant.id,
                    ConnectedAccount.platform != Platform.GDRIVE,
                )
            )
        )
        drive = db.scalar(
            select(ConnectedAccount).where(
                ConnectedAccount.workspace_id == tenant.id,
                ConnectedAccount.platform == Platform.GDRIVE,
            )
        )
        folder_mock(no_external_http)
        assert (
            save(
                client, db, plan, "source", {"wizard_step": "content", "drive_id": drive.id, "folder": FOLDER}
            ).status_code
            == 303
        )
        assert (
            save(
                client,
                db,
                plan,
                "targets",
                {"wizard_step": "accounts", "targets": [str(a.id) for a in accounts]},
            ).status_code
            == 303
        )
        assert (
            save(
                client,
                db,
                plan,
                "schedule",
                {
                    "wizard_step": "schedule",
                    "rotation_mode": "repeat",
                    "repeat_after_days": "7",
                    "timezone": "Africa/Cairo",
                    "start_date": (utcnow().date() + timedelta(days=1)).isoformat(),
                    "weekdays": [str(i) for i in range(7)],
                    "times": ["10:00"],
                    "ordering": "oldest",
                },
            ).status_code
            == 303
        )
        assert (
            save(
                client, db, plan, "rotation", {"rotation_mode": "repeat", "repeat_after_days": "7"}
            ).status_code
            == 303
        )

        library = client.get(f"/library?profile_id={plan.profile_id}")
        assert f'href="/people/{plan.profile_id}?section=preview"' in library.text
        assert f'href="/people/{plan.profile_id}?section=content"' in library.text
        text = post(
            client,
            "/library/text",
            {
                "profile_id": plan.profile_id,
                "title": "Synthetic text",
                "caption": "A customer’s first post",
            },
        )
        assert text.status_code == 303
        image = finish(db, upload(client, plan.profile_id, raster(), "image_post"))
        story = finish(db, upload(client, plan.profile_id, raster(color=(90, 120, 50, 200)), "image_story"))
        clip = video(db, plan, "Synthetic reel")
        db.commit()
        assert client.get(f"/library/{image.id}/preview").status_code == 200
        assert f'href="/people/{plan.profile_id}?section=preview"' in setup_card(client)
        for path in (
            f"/plans/{plan.id}",
            f"/plans/{plan.id}?page=2",
            f"/plans/{plan.id}/setup/preview",
            f"/plans/{plan.id}/setup/preview?page=2",
            f"/plans/{plan.id}/edit",
            "/schedule",
            "/settings/workspace",
        ):
            page = client.get(path)
            assert page.status_code == 200, path
        db.expire_all()  # HTTP saves use their own sessions; reload the selected account ownership.
        preview = simulate(db, plan)
        chosen = {e.video.id for row in preview["rows"] for e in row.entries if e.video}
        assert {image.id, story.id, clip.id}.issubset(chosen)
        assert preview["repeated"] > 0
        assert simulate(db, plan)["filled"] == preview["filled"]  # Viewing doesn't consume the library.
        assert save(client, db, plan, "execution", {"action": "start"}).status_code == 422

        # Operator and customer sessions never see or mutate each other's libraries/plans.
        for path in (
            f"/plans/{plan.id}",
            f"/plans/{plan.id}/edit",
            f"/library?profile_id={plan.profile_id}",
            f"/library/{image.id}",
        ):
            assert web_owner.get(path).status_code == 404
        assert plan.profile_id not in [
            v.profile_id for v in db.scalars(select(Video).where(Video.workspace_id == workspace.id))
        ]
        db.expire_all()
        assert not plan.enabled and tenant.paused and not get_settings().publishing_enabled
        assert not db.scalar(
            select(func.count())
            .select_from(Source)
            .where(
                Source.enabled.is_(True),
                Source.kind != SourceKind.MANUAL,
            )
        )
        assert not db.scalar(
            select(func.count()).select_from(RotationRule).where(RotationRule.enabled.is_(True))
        )
        assert not db.scalar(select(func.count()).select_from(ScheduledPost))
        assert not db.scalar(
            select(func.count())
            .select_from(Job)
            .where(Job.kind.in_(("publish_post", "poll_post", "poll_source")))
        )
        assert len(no_external_http.calls) == 1  # Only the mocked folder access check.


def test_setup_readiness_is_person_specific_and_requires_selected_active_pages(web_owner, db, make_account):
    set_lang(web_owner, "en")
    older = create_plan(web_owner, db, "Other person")
    video(db, older, "Other person's content")
    plan = create_plan(web_owner, db, "Current person")
    account = make_account(Platform.FACEBOOK)
    db.commit()
    assert setup_card(web_owner).count("Done") == 0
    save(web_owner, db, plan, "targets", {"targets": [str(account.id)]})
    assert setup_card(web_owner).count("Done") == 1
    item = video(db, plan, "Still processing")
    item.status = VideoStatus.PROCESSING
    db.commit()
    assert setup_card(web_owner).count("Done") == 1
    item.status = VideoStatus.READY
    db.commit()
    assert setup_card(web_owner).count("Done") == 2
    from tests.test_setup_wizard import schedule_data

    save(web_owner, db, plan, "schedule", schedule_data())
    assert setup_card(web_owner).count("Done") == 3
    assert "Preview 30 days" in setup_card(web_owner)
    account.status = AccountStatus.NEEDS_RECONNECT
    db.commit()
    assert setup_card(web_owner).count("Done") == 1


def test_text_validation_keeps_person_and_long_escaped_draft(web_owner, db):
    plan = create_plan(web_owner, db)
    body = "<script>not executable</script>\n" + "نص تجريبي طويل " * 250
    response = post(web_owner, "/library/text", {"profile_id": plan.profile_id, "title": "", "caption": body})
    assert response.status_code == 422
    assert html.escape(body) in response.text
    assert '<details id="compose" class="card" open>' in response.text
    assert f'name="profile_id" value="{plan.profile_id}"' in response.text
    assert f'href="/people/{plan.profile_id}?section=preview"' in response.text
    assert not db.scalar(select(func.count()).select_from(Video))
    assert len(str(web_owner.cookies)) < 4000
    assert (
        post(
            web_owner, "/library/text", {"profile_id": plan.profile_id, "title": "Fixed", "caption": body}
        ).status_code
        == 303
    )


def test_upload_validation_keeps_metadata_format_and_person(web_owner, db):
    set_lang(web_owner, "en")
    plan = create_plan(web_owner, db)
    response = upload(web_owner, plan.profile_id, b"not an image", "image_story")
    assert response.status_code == 422
    assert 'value="Synthetic image"' in response.text and "A caption</textarea>" in response.text
    assert '<option value="image_story" selected>' in response.text
    assert 'id="upload" class="card upload" open' in response.text
    assert "Choose the file again to retry" in response.text
    assert not db.scalar(select(func.count()).select_from(Video))


def test_edit_error_keeps_caption_placement_and_platform_choices(web_owner, db):
    plan = create_plan(web_owner, db)
    item = video(db, plan, "Original")
    db.commit()
    data = {
        "title": "x" * 301,
        "caption": "A draft <with> markup",
        "content_format": "story",
        "platform_facebook": "1",
    }
    response = post(web_owner, f"/library/{item.id}/edit", data)
    assert response.status_code == 422
    assert "A draft &lt;with&gt; markup</textarea>" in response.text
    assert '<option value="story" selected>' in response.text
    assert 'name="platform_facebook" value="1" checked' in response.text
    assert 'name="platform_instagram" value="1" checked' not in response.text
    db.refresh(item)
    assert item.content_format == "reel" and not item.overrides
    assert post(web_owner, f"/library/{item.id}/edit", {**data, "title": "Corrected"}).status_code == 303
    db.refresh(item)
    assert item.content_format == "story" and item.overrides["caption"] == data["caption"]


def test_existing_past_start_date_does_not_block_schedule_edits(web_owner, db):
    plan = create_plan(web_owner, db)
    old = utcnow().date() - timedelta(days=45)
    plan.start_date = old
    db.commit()
    data = {
        "timezone": "Africa/Cairo",
        "start_date": old.isoformat(),
        "weekdays": ["1", "4"],
        "times": ["15:00"],
        "ordering": "newest",
    }
    assert save(web_owner, db, plan, "schedule", data).status_code == 303
    db.refresh(plan)
    assert plan.start_date == old and plan.times == ["15:00"] and plan.weekdays == [1, 4]
    assert all(row.at > utcnow() for row in simulate(db, plan)["rows"])
    assert (
        save(
            web_owner, db, plan, "schedule", {**data, "start_date": (old - timedelta(days=1)).isoformat()}
        ).status_code
        == 422
    )


@pytest.mark.parametrize("chosen", [None, "ar"])
def test_invitation_language_defaults_to_workspace_but_keeps_explicit_choice(
    web_client, db, workspace, admin_user, chosen
):
    workspace.locale = "en"
    db.commit()
    if chosen:
        set_lang(web_client, chosen)
    _, url = invite(db, workspace, admin_user)
    open_private(web_client, url)
    assert f'<html lang="{chosen or "en"}"' in web_client.get("/invite/accept").text
