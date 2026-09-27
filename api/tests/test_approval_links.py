"""One-tap approval links in the brief email.

Committed, like test_newsletter: the Executive charter is applied and the
brief writer really runs. Jev, the model and Resend are scripted.
"""

import json
import re
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.approvals.links import token_hash
from app.brain.write_gate import WriteResult
from app.config import Settings, get_settings
from app.db import acting_as, as_service_role, connect
from app.mail import MailingListChange
from app.main import app
from app.owner_api import get_mailer, get_writer_factory
from tests.test_chief_of_staff import Office, office  # noqa: F401  (a fixture)
from tests.test_newsletter import OWNER_LIST, Resend, brief, email_row, mailing_list

BASE = "https://api.example.com"
LINKED = OWNER_LIST.model_copy(
    update={"send_without_approval": True, "approval_links_url": BASE + "/"}
)
LINK = re.compile(re.escape(BASE) + r"/a/([A-Za-z0-9_-]+)")


@dataclass
class Admitter:
    """Stands in for the brain writer; records what was admitted."""

    admitted: list[dict[str, Any]] = field(default_factory=list)

    def admit_approved(self, approval: dict[str, Any], *, agent_id: Any) -> WriteResult:
        self.admitted.append(approval)
        return WriteResult("accepted", ("Approved by the owner",))


def held(dsn: str, where: Office, action_type: str = "tool_call", **payload: Any) -> str:
    with connect(dsn) as connection, as_service_role(connection) as conn:
        return str(
            conn.execute(
                "insert into public.approvals (org_id, action_type, action_key, payload, "
                "explanation) values (%s, %s, %s, %s, 'Spends credits') returning id",
                (
                    str(where.org_id),
                    action_type,
                    f"tool:{payload.get('tool', 'fact')}",
                    json.dumps(payload),
                ),
            ).fetchone()["id"]
        )


def approval(dsn: str, approval_id: str) -> dict[str, Any]:
    with connect(dsn) as connection, as_service_role(connection) as conn:
        return conn.execute(
            "select * from public.approvals where id = %s", (approval_id,)
        ).fetchone()


@pytest.fixture
def web(dsn: str) -> Iterator[tuple[TestClient, Resend, Admitter]]:
    resend, admitter = Resend(), Admitter()
    app.dependency_overrides[get_settings] = lambda: Settings(database_url=dsn)
    app.dependency_overrides[get_mailer] = lambda: resend
    app.dependency_overrides[get_writer_factory] = lambda: lambda _conn, _agent: admitter
    yield TestClient(app), resend, admitter
    app.dependency_overrides.clear()


def linked_brief(dsn: str, where: Office) -> tuple[Resend, list[str]]:
    mailing_list(dsn, where, LINKED)
    resend = Resend()
    brief(dsn, where, resend)
    (sent,) = resend.sent
    return resend, LINK.findall(sent.text)


def test_the_brief_ends_with_one_link_per_waiting_approval(
    dsn: str,
    office: Office,  # noqa: F811
) -> None:
    tool = held(dsn, office, tool="mcp_higgsfield_generate_image", arguments={"prompt": "fort"})
    fact = held(dsn, office, "fact_write", claim="Acme raised a seed round", source="web:x")

    resend, tokens = linked_brief(dsn, office)

    (sent,) = resend.sent
    assert len(tokens) == 2
    assert "Decide from here" in sent.text and "Decide from here" in sent.html
    assert sent.html.count(f'href="{BASE}/a/') == 2
    assert "Fact: Acme raised a seed round" in sent.text
    # The tokens are only in what went out: the stored email and the link rows
    # (hashes) never hold one, so no agent can read a token.
    with connect(dsn) as connection, as_service_role(connection) as conn:
        email = conn.execute("select * from public.emails").fetchone()
        rows = conn.execute("select * from public.approval_links").fetchall()
        minted = conn.execute(
            "select payload from public.events where type = 'approval_links_minted'"
        ).fetchone()
    assert "/a/" not in email["body_text"] and "/a/" not in email["body_html"]
    assert {str(r["approval_id"]) for r in rows} == {tool, fact}
    assert {r["token_hash"] for r in rows} == {token_hash(t) for t in tokens}
    assert {r["user_id"] for r in rows} == {office.user_id}
    assert set(minted["payload"]["approvals"]) == {tool, fact}


def test_opening_a_link_decides_nothing_and_a_button_decides_once(
    dsn: str,
    office: Office,  # noqa: F811
    web: tuple[TestClient, Resend, Admitter],
) -> None:
    client, _, _ = web
    tool = held(dsn, office, tool="mcp_higgsfield_generate_image", arguments={"prompt": "fort"})
    _, (token,) = linked_brief(dsn, office)

    page = client.get(f"/a/{token}")
    assert page.status_code == 200
    assert "mcp_higgsfield_generate_image" in page.text and "Approve" in page.text
    assert page.headers["cache-control"] == "no-store"
    assert page.headers["referrer-policy"] == "no-referrer"
    assert approval(dsn, tool)["status"] == "pending"

    done = client.post(f"/a/{token}", data={"decision": "approve"})
    assert done.status_code == 200, done.text
    assert "Approved" in done.text
    decided = approval(dsn, tool)
    assert decided["status"] == "approved" and decided["decided_by"] == office.user_id

    # Used: pressing again, or opening again, does nothing.
    assert client.post(f"/a/{token}", data={"decision": "cancel"}).status_code == 410
    assert client.get(f"/a/{token}").status_code == 410
    assert approval(dsn, tool)["status"] == "approved"
    with connect(dsn) as connection, as_service_role(connection) as conn:
        used = conn.execute(
            "select payload from public.events where type = 'approval_link_used'"
        ).fetchone()
    assert used["payload"]["decision"] == "approve"


def test_expired_wrong_or_already_decided_links_do_nothing(
    dsn: str,
    office: Office,  # noqa: F811
    web: tuple[TestClient, Resend, Admitter],
) -> None:
    client, _, _ = web
    first = held(dsn, office, tool="a", arguments={})
    second = held(dsn, office, tool="b", arguments={})
    _, tokens = linked_brief(dsn, office)
    by_approval = {}
    with connect(dsn) as connection, as_service_role(connection) as conn:
        for row in conn.execute("select approval_id, token_hash from public.approval_links"):
            by_approval[str(row["approval_id"])] = next(
                t for t in tokens if token_hash(t) == row["token_hash"]
            )
        conn.execute(
            "update public.approval_links set expires_at = now() - interval '1 minute' "
            "where approval_id = %s",
            (first,),
        )
        conn.execute(
            "update public.approvals set status = 'rejected', decided_at = now() where id = %s",
            (second,),
        )

    for token in (by_approval[first], by_approval[second], "not-a-real-token"):
        assert client.get(f"/a/{token}").status_code == 410
        assert client.post(f"/a/{token}", data={"decision": "approve"}).status_code == 410
    assert approval(dsn, first)["status"] == "pending"


def test_sending_back_needs_a_note_and_a_bad_press_keeps_the_link(
    dsn: str,
    office: Office,  # noqa: F811
    web: tuple[TestClient, Resend, Admitter],
) -> None:
    client, _, _ = web
    tool = held(dsn, office, tool="save_draft", arguments={})
    _, (token,) = linked_brief(dsn, office)

    assert client.post(f"/a/{token}", data={"decision": "redirect"}).status_code == 400
    assert client.post(f"/a/{token}", data={"decision": "delete"}).status_code == 400
    assert client.get(f"/a/{token}").status_code == 200

    done = client.post(f"/a/{token}", data={"decision": "redirect", "note": "Shorter, please"})
    assert done.status_code == 200, done.text
    decided = approval(dsn, tool)
    assert decided["status"] == "rejected" and decided["verdict"] == "Shorter, please"


def test_approving_a_held_fact_from_a_link_stores_it(
    dsn: str,
    office: Office,  # noqa: F811
    web: tuple[TestClient, Resend, Admitter],
) -> None:
    client, _, admitter = web
    fact = held(dsn, office, "fact_write", claim="Acme raised a seed round", source="web:x")
    _, (token,) = linked_brief(dsn, office)

    assert "Admit to the brain" in client.get(f"/a/{token}").text
    assert client.post(f"/a/{token}", data={"decision": "redirect", "note": "x"}).status_code == (
        400
    )
    done = client.post(f"/a/{token}", data={"decision": "approve"})

    assert done.status_code == 200 and "Stored in the brain" in done.text
    (admitted,) = admitter.admitted
    assert str(admitted["id"]) == fact and admitted["status"] == "approved"


def test_no_links_unless_the_list_and_the_email_ask(
    dsn: str,
    office: Office,  # noqa: F811
) -> None:
    held(dsn, office, tool="a", arguments={})
    mailing_list(dsn, office, OWNER_LIST.model_copy(update={"send_without_approval": True}))
    resend = Resend()
    brief(dsn, office, resend)
    (sent,) = resend.sent
    assert "Decide from here" not in sent.text and not LINK.search(sent.text)

    # On for the list, but an email that did not ask (the evening question).
    mailing_list(dsn, office, LINKED)
    with connect(dsn) as connection, connection.cursor() as cursor:
        from app.mail import compose, send

        with as_service_role(connection):
            composed = compose(
                cursor,
                org_id=office.org_id,
                list_key="morning-brief",
                body="Anything for tomorrow?",
                idempotency_key=str(uuid.uuid4()),
            )
        send(connection, resend, composed.email_id)
    assert not LINK.search(resend.sent[-1].text)
    assert email_row(dsn, str(composed.email_id))["approval_links"] is False


def test_agents_cannot_see_links_and_list_changes_are_audited(
    dsn: str,
    office: Office,  # noqa: F811
) -> None:
    held(dsn, office, tool="a", arguments={})
    linked_brief(dsn, office)
    with connect(dsn) as connection:
        with as_service_role(connection) as conn:
            agent = conn.execute(
                "select id from public.agents where org_id = %s limit 1", (str(office.org_id),)
            ).fetchone()["id"]
            audit = conn.execute(
                "select payload from public.events where type like 'mailing_list_%%' "
                "and org_id = %s order by created_at desc limit 1",
                (str(office.org_id),),
            ).fetchone()
        with acting_as(connection, user_id=str(office.user_id), agent_id=str(agent)) as conn:
            assert (
                conn.execute("select count(*) as n from public.approval_links").fetchone()["n"] == 0
            )
        with acting_as(connection, user_id=str(office.user_id)) as conn:
            assert (
                conn.execute("select count(*) as n from public.approval_links").fetchone()["n"] == 1
            )
    assert audit["payload"]["approval_links_url"] == BASE


def test_a_list_refuses_a_non_https_address(
    dsn: str,
    office: Office,  # noqa: F811
) -> None:
    import psycopg

    with pytest.raises(psycopg.errors.CheckViolation):
        mailing_list(
            dsn,
            office,
            OWNER_LIST.model_copy(update={"approval_links_url": "http://api.example.com"}),
        )
    mailing_list(dsn, office, LINKED)
    mailing_list(dsn, office, MailingListChange(approval_links_url=""))
    with connect(dsn) as connection, as_service_role(connection) as conn:
        row = conn.execute(
            "select approval_links_url from public.mailing_lists where org_id = %s",
            (str(office.org_id),),
        ).fetchone()
    assert row["approval_links_url"] is None


def test_actions_are_linked_before_held_facts(
    dsn: str,
    office: Office,  # noqa: F811
) -> None:
    from app.approvals import links

    facts = [
        held(dsn, office, "fact_write", claim=f"Fact {i}", source="web:x")
        for i in range(links.MAX_LINKS)
    ]
    tool = held(dsn, office, tool="save_draft", arguments={})

    resend, tokens = linked_brief(dsn, office)

    (sent,) = resend.sent
    assert len(tokens) == links.MAX_LINKS and "And 1 more" in sent.text
    with connect(dsn) as connection, as_service_role(connection) as conn:
        linked = {
            str(r["approval_id"])
            for r in conn.execute("select approval_id from public.approval_links").fetchall()
        }
    assert tool in linked and facts[-1] not in linked
