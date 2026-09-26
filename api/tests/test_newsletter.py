"""The morning brief by email (ADR 028).

Committed, like test_chief_of_staff: the Executive charter is applied and the
brief writer really runs. Jev, the model and Resend are scripted.
"""

import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx
import psycopg
import pytest

from app.agents.autonomy import kill_everything
from app.agents.runs import Runtime
from app.approvals import decide
from app.brain import HashingEmbedder
from app.db import acting_as, as_service_role, connect
from app.mail import MailError, MailingListChange, Outgoing, ResendMailer, send, set_list
from app.tasks import order
from app.tracing import NullTracer
from tests.scripted_jev import ScriptedJev
from tests.test_chief_of_staff import (
    Office,
    Writer,
    office,  # noqa: F401  (a fixture)
    run_next,
)
from tests.test_runners import TIERS

OWNER_LIST = MailingListChange(
    from_address="The Unreal Lab <newsletter@example.com>",
    recipients=["Owner@Example.com"],
)


@dataclass
class Resend:
    """Records what would have been sent; fails on request."""

    sent: list[Outgoing] = field(default_factory=list)
    fail: bool = False

    def send(self, email: Outgoing) -> str:
        if self.fail:
            raise MailError("Resend refused the email (422): domain not verified")
        self.sent.append(email)
        return f"re_{len(self.sent)}"


def runtime(dsn: str, mailer: Resend | None) -> Runtime:
    return Runtime(
        dsn=dsn,
        transport=Writer(),
        tiers=TIERS,
        embedder=HashingEmbedder(),
        tracer=NullTracer(),
        systemone=ScriptedJev(),
        services={"mailer": mailer},
    )


def mailing_list(dsn: str, where: Office, change: MailingListChange = OWNER_LIST) -> None:
    with connect(dsn) as connection:
        set_list(
            connection,
            user_id=where.user_id,
            org_id=where.org_id,
            key="morning-brief",
            change=change,
        )


def brief(dsn: str, where: Office, mailer: Resend | None) -> dict[str, Any]:
    """The routine's task (it names the list), run once; returns its result."""
    with connect(dsn) as connection:
        task = order(
            connection,
            user_id=where.user_id,
            org_id=where.org_id,
            agent="brief-writer",
            title="Morning brief",
            input={"mailing_list": "morning-brief"},
            idempotency_key=str(uuid.uuid4()),
        )
    report = run_next(dsn, runtime(dsn, mailer))
    assert report.status == "succeeded", report.error
    with connect(dsn) as connection, as_service_role(connection) as conn:
        return conn.execute(
            "select result from public.tasks where id = %s", (str(task.id),)
        ).fetchone()["result"]


def email_row(dsn: str, email_id: str) -> dict[str, Any]:
    with connect(dsn) as connection, as_service_role(connection) as conn:
        return conn.execute("select * from public.emails where id = %s", (email_id,)).fetchone()


def test_a_list_left_on_approval_holds_the_brief_until_the_owner_approves(
    dsn: str,
    office: Office,  # noqa: F811
) -> None:
    mailing_list(dsn, office)
    resend = Resend()

    result = brief(dsn, office, resend)

    assert result["email"]["status"] == "held" and resend.sent == []
    email = email_row(dsn, result["email"]["id"])
    assert email["recipients"] == ["owner@example.com"]
    with connect(dsn) as connection:
        decided = decide(
            connection,
            user_id=office.user_id,
            approval_id=email["approval_id"],
            decision="approve",
            mailer=resend,
        )
    assert decided["email_status"] == "sent"
    (sent,) = resend.sent
    assert sent.recipients == ["owner@example.com"]
    assert sent.subject.startswith("Morning brief, ")
    assert "one approval waits for you" in sent.html
    assert sent.idempotency_key == f"pantheon-email-{email['id']}"
    # Sending again, or approving again, sends nothing more.
    with connect(dsn) as connection:
        assert send(connection, resend, email["id"]) == "sent"
    assert len(resend.sent) == 1


def test_a_list_the_owner_let_go_on_its_own_is_sent_after_the_run(
    dsn: str,
    office: Office,  # noqa: F811
) -> None:
    mailing_list(dsn, office, OWNER_LIST.model_copy(update={"send_without_approval": True}))
    resend = Resend()

    result = brief(dsn, office, resend)

    assert result["email"]["status"] == "sent"
    assert email_row(dsn, result["email"]["id"])["status"] == "sent"
    assert [e.recipients for e in resend.sent] == [["owner@example.com"]]


def test_no_list_means_no_email_and_the_brief_still_arrives(
    dsn: str,
    office: Office,  # noqa: F811
) -> None:
    result = brief(dsn, office, Resend())

    assert result["summary"] == "Brief: one approval waits for you."
    assert result["email"] == {
        "list": "morning-brief",
        "id": None,
        "status": "skipped",
        "reason": "no mailing list 'morning-brief'",
    }


def test_a_failed_send_is_recorded_and_can_be_retried(
    dsn: str,
    office: Office,  # noqa: F811
) -> None:
    mailing_list(dsn, office, OWNER_LIST.model_copy(update={"send_without_approval": True}))

    result = brief(dsn, office, Resend(fail=True))

    email = email_row(dsn, result["email"]["id"])
    assert email["status"] == "failed" and "domain not verified" in email["error"]
    retry = Resend()
    with connect(dsn) as connection:
        assert send(connection, retry, email["id"]) == "sent"
    assert len(retry.sent) == 1


def test_a_cancel_or_the_kill_cancels_a_held_email(
    dsn: str,
    office: Office,  # noqa: F811
) -> None:
    mailing_list(dsn, office)
    first = email_row(dsn, brief(dsn, office, Resend())["email"]["id"])
    second = email_row(dsn, brief(dsn, office, Resend())["email"]["id"])

    with connect(dsn) as connection:
        decide(
            connection,
            user_id=office.user_id,
            approval_id=first["approval_id"],
            decision="cancel",
        )
        kill_everything(connection, user_id=office.user_id, org_id=office.org_id, note="test")
        # Nothing cancelled can be sent.
        assert send(connection, Resend(), first["id"]) == "cancelled"

    assert email_row(dsn, first["id"])["status"] == "cancelled"
    assert email_row(dsn, second["id"])["status"] == "cancelled"


def _brief_writer(dsn: str, where: Office) -> uuid.UUID:
    with connect(dsn) as connection, as_service_role(connection) as conn:
        return conn.execute(
            "select id from public.agents where org_id = %s and name = 'brief-writer'",
            (str(where.org_id),),
        ).fetchone()["id"]


def test_an_agent_cannot_skip_approval_pick_recipients_or_edit_a_list(
    dsn: str,
    office: Office,  # noqa: F811
) -> None:
    mailing_list(dsn, office)
    agent = _brief_writer(dsn, office)
    insert = (
        "insert into public.emails (org_id, list_key, status, from_address, recipients, "
        "subject, body_text, body_html, idempotency_key) "
        "values (%s, 'morning-brief', %s, %s, %s, 's', 'b', 'b', %s)"
    )
    attempts = [
        (
            "ready",
            OWNER_LIST.from_address,
            ["owner@example.com"],
            psycopg.errors.InsufficientPrivilege,
        ),
        ("held", OWNER_LIST.from_address, ["owner@example.com"], psycopg.errors.CheckViolation),
        ("held", OWNER_LIST.from_address, ["someone@else.com"], psycopg.errors.CheckViolation),
        (
            "sent",
            OWNER_LIST.from_address,
            ["owner@example.com"],
            psycopg.errors.InsufficientPrivilege,
        ),
    ]
    with connect(dsn) as connection:
        for status, sender, to, refused in attempts:
            with pytest.raises((refused, psycopg.errors.CheckViolation)):
                with acting_as(connection, user_id=str(office.user_id), agent_id=str(agent)) as c:
                    c.execute(insert, (str(office.org_id), status, sender, to, str(uuid.uuid4())))
        with acting_as(connection, user_id=str(office.user_id), agent_id=str(agent)) as c:
            changed = c.execute(
                "update public.mailing_lists set recipients = '{x@evil.com}' returning id"
            ).fetchall()
        assert changed == [], "the list's policy hides it from agents' writes"


def test_list_changes_are_checked_and_audited(
    dsn: str,
    office: Office,  # noqa: F811
) -> None:
    mailing_list(dsn, office)
    with connect(dsn) as connection:
        with pytest.raises(psycopg.errors.CheckViolation, match="Not an email address"):
            set_list(
                connection,
                user_id=office.user_id,
                org_id=office.org_id,
                key="morning-brief",
                change=MailingListChange(recipients=["not-an-address"]),
            )
        row = set_list(
            connection,
            user_id=office.user_id,
            org_id=office.org_id,
            key="morning-brief",
            change=MailingListChange(recipients=["owner@example.com", "b@example.com"]),
        )
        assert row["from_address"] == OWNER_LIST.from_address, "untouched fields stay"
        with as_service_role(connection) as conn:
            events = conn.execute(
                "select type from public.events where org_id = %s and type like 'mailing_list_%%' "
                "order by created_at",
                (str(office.org_id),),
            ).fetchall()
    assert [e["type"] for e in events] == ["mailing_list_insert", "mailing_list_update"]


def test_resend_is_called_as_its_docs_say(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def post(url: str, *, json: dict[str, Any], headers: dict[str, str], timeout: float) -> Any:
        seen.update(url=url, json=json, headers=headers)
        return httpx.Response(200, json={"id": "49a3999c"}, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", post)
    provider_id = ResendMailer(api_key="re_test").send(
        Outgoing(
            from_address="Lab <newsletter@example.com>",
            recipients=["a@example.com"],
            subject="Morning brief",
            text="Hello",
            html="<p>Hello</p>",
            idempotency_key="pantheon-email-1",
            reply_to="owner@example.com",
        )
    )

    assert provider_id == "49a3999c"
    assert seen["url"] == "https://api.resend.com/emails"
    assert seen["headers"] == {
        "Authorization": "Bearer re_test",
        "Idempotency-Key": "pantheon-email-1",
    }
    assert (
        seen["json"]["to"] == ["a@example.com"] and seen["json"]["reply_to"] == "owner@example.com"
    )

    def refuse(url: str, **_: Any) -> Any:
        return httpx.Response(
            403, json={"message": "The domain is not verified"}, request=httpx.Request("POST", url)
        )

    monkeypatch.setattr(httpx, "post", refuse)
    with pytest.raises(MailError, match=r"403.*not verified"):
        ResendMailer(api_key="re_test").send(
            Outgoing("a <b@c.com>", ["d@e.com"], "s", "t", "h", "k")
        )
