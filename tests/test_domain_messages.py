"""Structured messages survive persistence and render in the viewer's language."""

from types import SimpleNamespace

import pytest
from sqlalchemy import select

from repeater.activity import log_activity
from repeater.models import ActivityLevel, ActivityLog, Video, VideoStatus
from repeater.web.i18n import Translator


def test_activity_uses_a_stable_key_after_english_wording_changes(db, workspace):
    from repeater.messages import msg

    event = log_activity(
        db,
        workspace.id,
        ActivityLevel.INFO,
        "import",
        msg("import.uploaded", "New English wording: {title}", title="My clip"),
    )
    db.commit()
    db.expire_all()
    event = db.scalars(select(ActivityLog)).one()
    assert event.data["msg_key"] == "import.uploaded"
    assert "My clip" in Translator("ar").activity(event)
    assert "New English wording" not in Translator("ar").activity(event)
    assert Translator("en").activity(event) == "New English wording: My clip"


def test_nested_platform_failure_roundtrips_and_error_clear_removes_metadata(db, workspace):
    from repeater.messages import msg
    from repeater.platforms.base import AuthError

    error = AuthError("Stored sign-in rejected", code="invalid_token")
    v = Video(
        workspace_id=workspace.id,
        status=VideoStatus.FAILED,
        title="clip",
        error=msg(
            "import.failed",
            "Video '{title}' could not be imported: {detail}",
            title="clip",
            detail=error.message,
        ),
    )
    db.add(v)
    db.commit()
    db.expire_all()
    stored = db.get(Video, v.id)
    translated = Translator("ar").error(stored.error, stored.error_info)
    assert "clip" in translated and "invalid_token" in translated
    assert "Stored sign-in" not in translated and "could not be imported" not in translated
    assert "Stored sign-in rejected" in Translator("en").error(stored.error, stored.error_info)
    stored.error = None
    db.commit()
    assert stored.error_info is None


def test_unknown_legacy_activity_keeps_its_original_diagnostic():
    assert (
        Translator("ar").activity(SimpleNamespace(data={}, message="legacy diagnostic"))
        == "legacy diagnostic"
    )


def test_upload_refusal_is_keyed_and_not_matched_by_english_text(db, workspace, monkeypatch):
    from repeater.sources import manual
    from repeater.web.messages import known_error

    upload = manual.storage.uploads_dir() / "invalid.mp4"
    upload.write_bytes(b"invalid")
    monkeypatch.setattr(manual, "probe", lambda _: (_ for _ in ()).throw(ValueError("new probe wording")))
    with pytest.raises(ValueError) as caught:
        manual.create_video_from_upload(
            db, workspace_id=workspace.id, upload_path=upload, original_filename="bad.mp4"
        )
    assert known_error(caught.value)[0] == "err.upload_unsupported"
    assert "bad.mp4" in Translator("ar").error(caught.value)
    assert "new probe wording" not in Translator("ar").error(caught.value)
    assert not upload.exists()


def test_arabic_telegram_alerts_follow_workspace_locale_and_keep_private_data_local(
    db, workspace, monkeypatch
):
    import json

    import respx

    from repeater import notify
    from repeater.config import get_settings
    from repeater.messages import msg

    workspace.locale = "ar"
    db.commit()
    monkeypatch.setattr(get_settings(), "telegram_bot_token", "local-test-token")
    monkeypatch.setattr(get_settings(), "telegram_chat_id", "local-test-chat")
    text = msg(
        "import.failed",
        "Video '{title}' could not be imported: {detail}",
        title="<Private title>",
        detail=msg("import.original_missing", "Original missing"),
    )
    with respx.mock as router:
        route = router.post("https://api.telegram.org/botlocal-test-token/sendMessage").respond(
            200, json={"ok": True}
        )
        assert notify.notify(text, workspace_id=workspace.id, category="source_failing")
        minimal = json.loads(route.calls[-1].request.content)["text"]
        assert "مصادر" in minimal and "Private" not in minimal
        monkeypatch.setattr(get_settings(), "telegram_include_details", True)
        assert notify.notify(text, workspace_id=workspace.id, category="source_failing")
        detail = json.loads(route.calls[-1].request.content)["text"]
        assert "تعذّر استيراد" in detail and "&lt;Private title&gt;" in detail
        assert "could not be imported" not in detail and "Original missing" not in detail


def test_structured_message_scrubbing_removes_urls_from_nested_persisted_parameters():
    from repeater.engine.publisher import _scrub
    from repeater.messages import msg
    from repeater.platforms.base import RetryableError

    detail = RetryableError("Upload failed at https://secret.invalid/token").message
    clean = _scrub(msg("import.failed", "{title}: {detail}", title="clip", detail=detail))
    assert "secret.invalid" not in str(clean.info)


def test_message_producers_supply_every_arabic_placeholder():
    import ast
    from pathlib import Path
    from string import Formatter

    from repeater.message_catalog import AR

    root = Path(__file__).resolve().parents[1] / "repeater"
    for path in root.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "Message"
                and node.args
                and isinstance(node.args[0], ast.Constant)
            ):
                continue
            key = node.args[0].value
            assert key in AR, (path, node.lineno, key)
            if len(node.args) > 2 and isinstance(node.args[2], ast.Dict):
                supplied = {k.value for k in node.args[2].keys}
            else:
                supplied = set()
            required = {field for _, field, _, _ in Formatter().parse(AR[key]) if field}
            assert required <= supplied, (path, node.lineno, key, required - supplied)


def test_channel_refresh_redacts_names_inside_nested_message_parameters(db, make_account):
    from repeater.accounts import redact_name_in_activity
    from repeater.messages import msg

    name = "Old private channel"
    account = make_account("youtube", display_name=name)
    entry = log_activity(
        db,
        account.workspace_id,
        ActivityLevel.ERROR,
        "publish",
        msg(
            "import.failed",
            "{title}: {detail}",
            title="clip",
            detail=msg(
                "account.reconnect_action", "Reconnect {platform} {name}", platform="YouTube", name=name
            ),
        ),
        account_id=account.id,
    )
    db.commit()
    assert redact_name_in_activity(db, account, name) == 1
    db.commit()
    db.expire_all()
    entry = db.get(ActivityLog, entry.id)
    assert name not in entry.message and name not in str(entry.data)
    assert name not in Translator("ar").activity(entry)
