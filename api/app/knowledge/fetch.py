"""Fetching a web page safely (Step 6, owner decision 9: plain HTTP, free).

A URL someone pastes can point anywhere, including inside our own network
(cloud metadata services, the database, localhost). So:

- https only, on the default port, with no credentials in the URL;
- the host must resolve only to public addresses; private, loopback,
  link-local, reserved and multicast addresses are refused;
- redirects are followed by hand, at most five, and every hop is checked the
  same way;
- at most 2 MB is read, within 10 seconds, and only HTML or plain text is
  accepted.

No JavaScript is run, so pages that build their text in the browser come
back thin. A paid rendering service needs the owner's approval first.
"""

import ipaddress
import socket
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

import httpx

from app.knowledge.extract import HTML_TYPES, TEXT_TYPES, base_type

MAX_BYTES = 2_000_000
MAX_REDIRECTS = 5
TIMEOUT_SECONDS = 10.0
USER_AGENT = "PantheonBot/0.1 (+owner-operated research agent)"

Resolver = Callable[[str], list[str]]


class FetchRefused(ValueError):
    """The URL or the response is not one we will read."""


@dataclass(frozen=True)
class FetchedPage:
    url: str
    final_url: str
    content: bytes
    content_type: str


def system_resolver(host: str) -> list[str]:
    try:
        infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except socket.gaierror as error:
        raise FetchRefused(f"Cannot resolve {host}: {error}") from error
    return sorted({info[4][0] for info in infos})


def check_url(url: str, resolve: Resolver = system_resolver) -> str:
    """The URL, if it is safe to fetch; otherwise FetchRefused."""
    parts = urlsplit(url.strip())
    if parts.scheme != "https":
        raise FetchRefused("Only https links are fetched")
    if parts.username or parts.password:
        raise FetchRefused("Links with credentials are refused")
    if parts.port not in (None, 443):
        raise FetchRefused("Only the default https port is fetched")
    host = parts.hostname
    if not host:
        raise FetchRefused("The link has no host")
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        addresses = resolve(host)
    else:
        addresses = [str(literal)]
    if not addresses:
        raise FetchRefused(f"{host} resolves to nothing")
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if not ip.is_global or ip.is_multicast:
            raise FetchRefused(f"{host} points at a non-public address ({address})")
    return url.strip()


def fetch(
    url: str,
    *,
    client: httpx.Client | None = None,
    resolve: Resolver = system_resolver,
) -> FetchedPage:
    http = client or httpx.Client(timeout=TIMEOUT_SECONDS, follow_redirects=False)
    current = check_url(url, resolve)
    for _ in range(MAX_REDIRECTS + 1):
        try:
            with http.stream(
                "GET", current, headers={"user-agent": USER_AGENT, "accept": "text/html,text/plain"}
            ) as response:
                if response.is_redirect:
                    location = response.headers.get("location")
                    if not location:
                        raise FetchRefused("A redirect with no destination")
                    current = check_url(urljoin(current, location), resolve)
                    continue
                if response.status_code >= 400:
                    raise FetchRefused(f"The page answered {response.status_code}")
                content_type = response.headers.get("content-type", "")
                if base_type(content_type) not in HTML_TYPES | TEXT_TYPES:
                    raise FetchRefused(f"Not a web page ({base_type(content_type) or 'no type'})")
                body = bytearray()
                for piece in response.iter_bytes():
                    body.extend(piece)
                    if len(body) > MAX_BYTES:
                        raise FetchRefused(f"The page is over {MAX_BYTES} bytes")
                return FetchedPage(url, current, bytes(body), content_type)
        except httpx.HTTPError as error:
            raise FetchRefused(f"Could not fetch the page: {error}") from error
    raise FetchRefused(f"More than {MAX_REDIRECTS} redirects")
