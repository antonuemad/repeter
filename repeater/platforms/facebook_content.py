"""Facebook Page text/photos and photo/video Stories.

Final feed/photo/story writes are never blindly retried. A lost direct-post response is
needs-attention; a lost video-story finish response is resolved with read-only status polls.
Contracts: Meta's facebook-python-business-sdk Page.create_feed/create_photo/
create_photo_story/create_video_story (checked 2026-10-16). No personal-profile publishing.
"""

from urllib.parse import urlsplit

from repeater.platforms import meta, rich_media
from repeater.platforms.base import (
    PermanentError,
    PlatformError,
    PublishOutcome,
    PublishResult,
    RetryableError,
    UnknownOutcomeError,
)


def validate(account, req):
    from repeater.platforms.instagram import _public_url_problem

    issues = rich_media.problems(account, req)
    if not (account.extra.get("page_id") or account.external_id):
        issues.append("Reconnect this Facebook Page; its ID is missing.")
    if req.media_kind == "image":
        issue = _public_url_problem(req.public_url)
        if issue:
            issues.append(issue)
    return issues


def _final_write(graph, path, data, *, state=None, secrets=()):
    from repeater.platforms.facebook import _may_have_landed

    try:
        body = graph.post(path, data, action="publishing Facebook content", secrets=secrets)
    except PlatformError as exc:
        if _may_have_landed(exc):
            raise UnknownOutcomeError(
                "Facebook did not confirm publication; do not send it again.", state=state
            ) from exc
        raise
    if body.get("success") is False:
        raise PermanentError("Facebook refused publication.", code="publish_refused")
    if not (body.get("id") or body.get("post_id") or body.get("success") is True):
        raise UnknownOutcomeError("Facebook returned no publication confirmation.", state=state)
    return body


def _published(body, *, fallback=None):
    identity = body.get("post_id") or body.get("id") or fallback
    return PublishResult(
        PublishOutcome.PUBLISHED,
        platform_post_id=str(identity) if identity else None,
        message="Published on Facebook.",
    )


def publish(account, req):
    from repeater.platforms.facebook import _file_size, _page_id, _upload

    rich_media.require_publishing()
    issues = validate(account, req)
    if issues:
        raise PermanentError(" ".join(issues), code="invalid_content")
    if req.state:
        # No new upload if the engine supplies a previous attempt's adapter state.
        return PublishResult(PublishOutcome.PROCESSING, state=dict(req.state), poll_after_seconds=30)
    page_id = _page_id(account)
    with meta.Graph(account.tokens.access_token) as graph:
        if req.media_kind == "text":
            return _published(_final_write(graph, f"{meta.node(page_id)}/feed", {"message": req.caption}))
        if req.media_kind == "image":
            if req.content_format == "post":
                return _published(
                    _final_write(
                        graph,
                        f"{meta.node(page_id)}/photos",
                        {"url": req.public_url, "caption": req.caption, "published": "true"},
                        secrets=(req.public_url,),
                    )
                )
            # An unpublished photo upload cannot create a live post. Only the next call can.
            body = graph.post(
                f"{meta.node(page_id)}/photos",
                {"url": req.public_url, "published": "false"},
                action="preparing a Facebook story photo",
                secrets=(req.public_url,),
            )
            photo_id = body.get("id")
            if not photo_id:
                raise RetryableError("Facebook returned no prepared photo ID.")
            return _published(
                _final_write(graph, f"{meta.node(page_id)}/photo_stories", {"photo_id": str(photo_id)})
            )
        # Video story: use Meta's returned, strictly allowlisted upload URL.
        body = graph.post(
            f"{meta.node(page_id)}/video_stories",
            {"upload_phase": "start"},
            action="starting a Facebook video story",
        )
        video_id, upload_url = body.get("video_id"), body.get("upload_url")
        if not video_id or not isinstance(upload_url, str):
            raise RetryableError("Facebook returned no story upload session.")
        parts = urlsplit(upload_url)
        if (
            parts.scheme != "https"
            or parts.hostname != "rupload.facebook.com"
            or parts.port not in (None, 443)
            or parts.username
            or parts.password
            or parts.query
            or parts.fragment
            or not parts.path.endswith("/" + meta.node(str(video_id)))
        ):
            raise PermanentError(
                "Facebook returned an unexpected story upload address.", code="bad_upload_url"
            )
        _upload(
            graph,
            str(video_id),
            req.video_path,
            _file_size(req.video_path),
            account.tokens.access_token,
            upload_url=upload_url,
        )
        state = {"rich_kind": "video_story", "video_id": str(video_id), "page_id": page_id}
        _final_write(
            graph,
            f"{meta.node(page_id)}/video_stories",
            {"upload_phase": "finish", "video_id": str(video_id)},
            state=state,
        )
        return PublishResult(
            PublishOutcome.PROCESSING, platform_post_id=str(video_id), state=state, poll_after_seconds=30
        )


def poll(account, state):
    from repeater.platforms.facebook import _check_failures, _is_published, _read_status

    rich_media.require_publishing()
    if state.get("rich_kind") != "video_story" or not state.get("video_id"):
        raise PermanentError(
            "An ambiguous Facebook post needs manual verification.", code="publish_unconfirmed"
        )
    with meta.Graph(account.tokens.access_token) as graph:
        status = _read_status(graph, str(state["video_id"]))
    if _is_published(status):
        return PublishResult(
            PublishOutcome.PUBLISHED, platform_post_id=str(state["video_id"]), state=dict(state)
        )
    try:
        _check_failures(status)
    except PlatformError as exc:
        # A finish was sent. Never start a second story after a contradictory status response.
        raise PermanentError(
            "Facebook could not confirm this story; check the Page before retrying.",
            code="publish_unconfirmed",
        ) from exc
    return PublishResult(PublishOutcome.PROCESSING, state=dict(state), poll_after_seconds=30)
