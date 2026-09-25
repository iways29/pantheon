"""Plain text out of uploaded files and fetched pages. Standard library only.

Text formats are read as UTF-8; HTML has its scripts, styles and markup
removed. PDF and Word files need a parsing library, which is a new dependency
the owner has not approved, so they are refused with a clear message for now.
"""

import json
from html.parser import HTMLParser

TEXT_TYPES = {"text/plain", "text/markdown", "text/csv", "text/x-markdown"}
HTML_TYPES = {"text/html", "application/xhtml+xml"}
JSON_TYPES = {"application/json"}
SUPPORTED = TEXT_TYPES | HTML_TYPES | JSON_TYPES

_SKIP = {"script", "style", "noscript", "template", "svg", "head"}
_BLOCK = {
    "p",
    "div",
    "section",
    "article",
    "br",
    "li",
    "ul",
    "ol",
    "tr",
    "table",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "header",
    "footer",
    "main",
    "aside",
    "blockquote",
    "pre",
    "title",
}


class UnsupportedContent(ValueError):
    pass


def base_type(content_type: str) -> str:
    return content_type.split(";", 1)[0].strip().lower()


def extract_text(content: bytes, content_type: str) -> str:
    kind = base_type(content_type)
    if kind not in SUPPORTED:
        raise UnsupportedContent(
            f"{kind or 'unknown type'} is not supported yet; send text, Markdown, CSV, "
            "JSON or HTML (PDF and Word need a parser the owner has not approved)"
        )
    text = content.decode("utf-8", errors="replace")
    if kind in HTML_TYPES:
        return html_to_text(text)
    if kind in JSON_TYPES:
        try:
            return json.dumps(json.loads(text), indent=2, ensure_ascii=False)
        except json.JSONDecodeError as error:
            raise UnsupportedContent(f"Invalid JSON: {error}") from error
    return _tidy(text)


def html_to_text(html: str) -> str:
    parser = _TextParser()
    parser.feed(html)
    parser.close()
    return _tidy("".join(parser.parts))


class _TextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skipping = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIP:
            self._skipping += 1
        elif tag in _BLOCK:
            self.parts.append("\n\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP:
            self._skipping = max(0, self._skipping - 1)
        elif tag in _BLOCK:
            self.parts.append("\n\n")

    def handle_data(self, data: str) -> None:
        if not self._skipping:
            self.parts.append(data)

    def handle_comment(self, data: str) -> None:
        # Comments are invisible to people but not to models: keep them, so
        # the screening sees an injection hidden in one (ADR 011).
        if not self._skipping and data.strip():
            self.parts.append(f"\n\n<!-- {data.strip()} -->\n\n")


def _tidy(text: str) -> str:
    paragraphs = []
    for block in text.replace("\r\n", "\n").split("\n\n"):
        line = " ".join(block.split())
        if line:
            paragraphs.append(line)
    return "\n\n".join(paragraphs)
