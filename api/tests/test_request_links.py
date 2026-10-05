"""The topic form at the end of the brief email (owner, 2026-10-05).

Committed, like test_approval_links: the brief writer really runs; the model,
Jev and Resend are scripted.
"""

import re

from fastapi.testclient import TestClient

from app.db import as_service_role, connect
from app.mail.requests import token_hash
from tests.test_approval_links import BASE, LINKED, Admitter, web  # noqa: F401  (a fixture)
from tests.test_chief_of_staff import Office, office  # noqa: F401  (a fixture)
from tests.test_newsletter import Resend, brief, mailing_list

ROUTINES = ["research:morning-brief", "marketing:morning-draft"]
FORM = LINKED.model_copy(update={"request_routines": ROUTINES})
TOKEN = re.compile(re.escape(BASE) + r"/r/([A-Za-z0-9_-]+)")


def form_brief(dsn: str, where: Office) -> tuple[Resend, str]:
    mailing_list(dsn, where, FORM)
    resend = Resend()
    brief(dsn, where, resend)
    (sent,) = resend.sent
    tokens = set(TOKEN.findall(sent.html))
    assert len(tokens) == 1
    return resend, tokens.pop()


def requests_for(dsn: str, routine: str) -> list[dict]:
    with connect(dsn) as connection, as_service_role(connection) as conn:
        return conn.execute(
            "select * from public.routine_requests where routine_key = %s and status = 'pending' "
            "order by created_at",
            (routine,),
        ).fetchall()


def test_the_brief_ends_with_a_themed_form_that_posts_a_request(
    dsn: str,
    office: Office,  # noqa: F811
) -> None:
    resend, token = form_brief(dsn, office)

    (sent,) = resend.sent
    assert f'<form method="post" action="{BASE}/r/{token}"' in sent.html
    assert 'value="research:morning-brief" checked' in sent.html
    assert ">Submit for tomorrow</button>" in sent.html
    assert "#070b18" in sent.html, "in Pantheon's colours"
    assert f"{BASE}/r/{token}" in sent.text, "the text version links to the page"
    with connect(dsn) as connection, as_service_role(connection) as conn:
        email = conn.execute("select * from public.emails").fetchone()
        link = conn.execute("select * from public.request_links").fetchone()
    assert "/r/" not in email["body_html"], "no token is stored where an agent can read"
    assert link["token_hash"] == token_hash(token) and link["user_id"] == office.user_id
    assert list(link["routines"]) == ROUTINES


def test_submitting_adds_a_request_for_the_next_run_a_few_times_at_most(
    dsn: str,
    office: Office,  # noqa: F811
    web: tuple[TestClient, Resend, Admitter],  # noqa: F811
) -> None:
    client, _, _ = web
    _, token = form_brief(dsn, office)

    page = client.get(f"/r/{token}")
    assert page.status_code == 200 and "Ask for tomorrow" in page.text
    assert page.headers["cache-control"] == "no-store"
    assert requests_for(dsn, "research:morning-brief") == [], "opening adds nothing"

    done = client.post(
        f"/r/{token}",
        data={"routine": "research:morning-brief", "request": "  Who builds AI\nfor insurance?  "},
    )
    assert done.status_code == 200, done.text
    assert "Who builds AI for insurance?" in done.text
    (asked,) = requests_for(dsn, "research:morning-brief")
    assert asked["request"] == "Who builds AI for insurance?"
    assert asked["created_by"] == office.user_id

    marketing = client.post(
        f"/r/{token}", data={"routine": "marketing:morning-draft", "request": "A post on demos"}
    )
    assert marketing.status_code == 200
    assert len(requests_for(dsn, "marketing:morning-draft")) == 1

    # A routine the form does not offer goes to the first one it does.
    client.post(f"/r/{token}", data={"routine": "executive:morning-brief", "request": "Odd one"})
    assert len(requests_for(dsn, "executive:morning-brief")) == 0
    assert len(requests_for(dsn, "research:morning-brief")) == 2

    # Too short: refused, and no use is spent.
    assert client.post(f"/r/{token}", data={"request": "x"}).status_code == 400

    for n in range(2):
        assert client.post(f"/r/{token}", data={"request": f"Topic {n}"}).status_code == 200
    # Five sent: the form is spent.
    assert client.post(f"/r/{token}", data={"request": "One more"}).status_code == 410
    assert client.get(f"/r/{token}").status_code == 410
    with connect(dsn) as connection, as_service_role(connection) as conn:
        used = conn.execute(
            "select count(*) as n from public.events where type = 'request_link_used'"
        ).fetchone()
    assert used["n"] == 5


def test_an_expired_or_unknown_form_does_nothing(
    dsn: str,
    office: Office,  # noqa: F811
    web: tuple[TestClient, Resend, Admitter],  # noqa: F811
) -> None:
    client, _, _ = web
    _, token = form_brief(dsn, office)
    with connect(dsn) as connection, as_service_role(connection) as conn:
        conn.execute("update public.request_links set expires_at = now() - interval '1 minute'")

    for t in (token, "not-a-real-token"):
        assert client.get(f"/r/{t}").status_code == 410
        assert client.post(f"/r/{t}", data={"request": "Anything"}).status_code == 410
    assert requests_for(dsn, "research:morning-brief") == []


def test_no_form_unless_the_list_offers_routines(
    dsn: str,
    office: Office,  # noqa: F811
) -> None:
    mailing_list(dsn, office, LINKED)
    resend = Resend()
    brief(dsn, office, resend)
    (sent,) = resend.sent
    assert "<form" not in sent.html and not TOKEN.search(sent.text)
