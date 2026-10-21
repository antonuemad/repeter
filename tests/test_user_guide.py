"""The guide is discoverable, bilingual, tenant-safe and entirely read-only."""

from html import unescape
from html.parser import HTMLParser

import pytest
import respx
from sqlalchemy import func, select

from repeater.config import get_settings
from repeater.models import ContentPlan, CreatorProfile, Job, ScheduledPost, Source, Video, Workspace
from repeater.timeutil import utcnow
from repeater.web.guide_content import chapters_for
from tests.test_content_plans import create_plan
from tests.test_web_support import login, set_lang

pytest_plugins = ["tests.test_web_support"]


class Links(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.ids = []
        self.links = []
        self.forms = []
        self.section = None
        self.chapter_links = {}
        self.id_tags = {}
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "section" and attrs.get("class") == "guide-chapter":
            self.section = attrs["id"]
            self.chapter_links[self.section] = []
        if "id" in attrs:
            self.ids.append(attrs["id"])
            self.id_tags[attrs["id"]] = tag
        if tag == "a":
            self.links.append(attrs)
            if self.section:
                self.chapter_links[self.section].append(attrs)
        if tag == "form":
            self.forms.append(attrs.get("action"))

    def handle_endtag(self, tag):
        if tag == "section":
            self.section = None


def test_anonymous_guide_requires_login(web_client):
    response = web_client.get("/guide", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login?next=%2Fguide"


def test_guide_keeps_consent_gate(web_client):
    login(web_client, accept_terms=False)
    response = web_client.get("/guide", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/consent?next=%2Fguide"


@pytest.mark.parametrize("lang", ["ar", "en"])
@pytest.mark.parametrize("client_fixture", ["web_owner", "web_editor"])
def test_members_can_read_localized_guide_without_admin_sections(request, client_fixture, lang):
    client = request.getfixturevalue(client_fixture)
    set_lang(client, lang)
    response = client.get("/guide")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert f'<html lang="{lang}" dir="{"rtl" if lang == "ar" else "ltr"}">' in response.text
    doc = Links(response.text)
    assert len(doc.ids) == len(set(doc.ids))
    assert list(doc.chapter_links) == ["guide-topic"]
    assert "access" in doc.ids and "drive" not in doc.ids
    for chapter in chapters_for(lang, is_admin=False):
        assert any(
            link.get("data-guide-topic") == chapter["id"]
            and link["href"] == f"/guide?topic={chapter['id']}#guide-article"
            for link in doc.links
        )
    assert "administration" not in doc.ids and "operations" not in doc.ids
    assert not any(link["href"].startswith("/admin/") for link in doc.links)
    assert any(link.get("href") == "/guide" and link.get("aria-current") == "page" for link in doc.links)


@pytest.mark.parametrize("lang", ["ar", "en"])
def test_platform_admin_also_gets_customer_and_operations_instructions(web_owner, admin_user, db, lang):
    admin_user.is_platform_admin = True
    db.commit()
    set_lang(web_owner, lang)
    response = web_owner.get("/guide")
    assert response.status_code == 200
    doc = Links(response.text)
    for chapter in chapters_for(lang, is_admin=True):
        assert any(link.get("data-guide-topic") == chapter["id"] for link in doc.links)
    assert {"href": "/admin/operations"} in Links(web_owner.get("/guide?topic=operations").text).links
    assert {"href": "/admin/customers"} in Links(web_owner.get("/guide?topic=administration").text).links


@pytest.mark.parametrize("lang", ["ar", "en"])
@pytest.mark.parametrize("is_admin", [False, True])
def test_topics_work_without_javascript_and_previous_next_follow_allowed_sequence(
    web_owner, admin_user, db, lang, is_admin
):
    admin_user.is_platform_admin = is_admin
    db.commit()
    set_lang(web_owner, lang)
    chapters = chapters_for(lang, is_admin=is_admin)
    for position, chapter in enumerate(chapters):
        response = web_owner.get(f"/guide?topic={chapter['id']}")
        assert response.status_code == 200
        doc = Links(response.text)
        assert list(doc.chapter_links) == ["guide-topic"]
        assert doc.id_tags["guide-article"] == "h2"
        assert chapter["id"] in doc.ids
        assert chapter["title"] in unescape(response.text) and chapter["steps"][0] in unescape(response.text)
        assert response.headers["cache-control"] == "no-store"
        selected = [link for link in doc.links if link.get("data-guide-topic") and link.get("aria-current")]
        assert len(selected) == 1 and selected[0]["data-guide-topic"] == chapter["id"]
        previous = [link["href"] for link in doc.links if link.get("rel") == "prev"]
        following = [link["href"] for link in doc.links if link.get("rel") == "next"]
        assert previous == (
            [f"/guide?topic={chapters[position - 1]['id']}#guide-article"] if position else []
        )
        assert following == (
            [f"/guide?topic={chapters[position + 1]['id']}#guide-article"]
            if position + 1 < len(chapters)
            else []
        )


@pytest.mark.parametrize("topic", ["missing", "administration", "operations", "//evil.test", "<script>"])
def test_unknown_or_restricted_topic_does_not_leak_admin_content(web_owner, topic):
    response = web_owner.get("/guide", params={"topic": topic})
    assert response.status_code == 404
    assert "<script>" not in response.text


def test_selected_person_is_retained_in_task_links_and_never_accepted_from_query(web_owner, db):
    first = create_plan(web_owner, db, "First synthetic person")
    second = create_plan(web_owner, db, "Second synthetic person")
    web_owner.get(f"/people/{first.profile_id}?section=content")
    for topic, destination in {
        "accounts": f"/people/{first.profile_id}?section=accounts",
        "drive": f"/people/{first.profile_id}?section=content",
        "rotation": f"/people/{first.profile_id}?section=schedule",
        "preview": f"/people/{first.profile_id}?section=preview",
        "library": f"/library?profile_id={first.profile_id}",
        "calendar": f"/calendar?person={first.profile_id}",
        "connection-management": "/accounts",
    }.items():
        response = web_owner.get(f"/guide?topic={topic}&person={second.profile_id}")
        doc = Links(response.text)
        assert {"href": destination, "class": "button"} in doc.chapter_links["guide-topic"]
        assert f"/plans/{second.id}/setup" not in response.text


def test_deleted_person_falls_back_to_choosing_person(web_owner, db):
    plan = create_plan(web_owner, db)
    web_owner.get(f"/plans/{plan.id}/setup/content")
    db.delete(db.get(CreatorProfile, plan.profile_id))
    db.commit()
    response = web_owner.get("/guide?topic=drive")
    assert response.status_code == 200
    assert f"/plans/{plan.id}/setup" not in response.text
    assert {"href": "/plans"} in Links(response.text).links


def test_foreign_person_is_not_exposed_through_guide_query(web_owner, db):
    foreign_ws = Workspace(name="Private foreign workspace")
    db.add(foreign_ws)
    db.flush()
    person = CreatorProfile(workspace_id=foreign_ws.id, name="Secret foreign person")
    db.add(person)
    db.flush()
    plan = ContentPlan(
        workspace_id=foreign_ws.id, profile_id=person.id, timezone="UTC", start_date=utcnow().date()
    )
    db.add(plan)
    db.commit()
    response = web_owner.get(f"/guide?person={person.id}&plan={plan.id}")
    assert response.status_code == 200
    assert person.name not in response.text
    assert f"/plans/{plan.id}/setup" not in response.text


@pytest.mark.parametrize("lang", ["ar", "en"])
def test_guide_opens_no_provider_and_creates_no_work(web_owner, db, monkeypatch, lang):
    monkeypatch.setattr(get_settings(), "publishing_enabled", False)
    plan = create_plan(web_owner, db)
    web_owner.get(f"/plans/{plan.id}/setup/content")
    models = (ContentPlan, CreatorProfile, Source, Video, Job, ScheduledPost)
    before = [db.scalar(select(func.count()).select_from(model)) for model in models]
    set_lang(web_owner, lang)
    with respx.mock(assert_all_called=False) as mock:
        for topic in ("drive", "safety", "connection-management", "safety"):
            response = web_owner.get(f"/guide?topic={topic}")
            assert response.status_code == 200
        assert not mock.calls
    assert [db.scalar(select(func.count()).select_from(model)) for model in models] == before
    db.refresh(plan)
    assert not plan.enabled and not get_settings().publishing_enabled
    doc = Links(response.text)
    assert set(doc.forms) == {"/lang", "/theme", "/logout"}
    assert not any(
        link["href"].startswith("/connect/") or "publish-now" in link["href"] for link in doc.links
    )
    assert (
        "الخاص وغير المدرج" in response.text
        if lang == "ar"
        else "private, unlisted or test posts" in response.text
    )
    assert "رفع مسودات" in response.text if lang == "ar" else "no platform draft uploads" in response.text


@pytest.mark.parametrize("path", ["/", "/plans", "/setup", "/accounts", "/library", "/calendar"])
def test_guide_is_linked_from_customer_journey(web_owner, path):
    response = web_owner.get(path)
    assert response.status_code == 200
    assert 'href="/guide"' in response.text
