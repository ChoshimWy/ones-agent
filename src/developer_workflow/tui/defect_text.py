"""Plain, bounded display of saved ONES descriptions; never load HTML resources."""
from __future__ import annotations

import re
from html.parser import HTMLParser

from rich.markup import escape

from ..verification import public_text


class _DescriptionParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "head"}:
            self.hidden.append(tag)
        if self.hidden:
            return
        if tag in {"p", "div", "br", "li", "pre", "h1", "h2", "h3", "tr"}:
            self.parts.append("\n")
        if tag == "li":
            self.parts.append("• ")

    def handle_endtag(self, tag: str) -> None:
        if self.hidden:
            if tag == self.hidden[-1]:
                self.hidden.pop()
            return
        if tag in {"p", "div", "li", "pre", "h1", "h2", "h3", "tr"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.hidden:
            self.parts.append(data)


def defect_display_text(value: str, *, multiline: bool = False, maximum: int = 512) -> str:
    # Remove private-key blocks before clipping can drop their closing marker.
    # Other credential redaction runs after HTML parsing: running a value regex
    # across tags/entities can consume part of the next credential's key.
    value = re.sub(r"-----BEGIN [^-]*PRIVATE KEY-----.*?(?:-----END [^-]*PRIVATE KEY-----|\Z)",
                   "[redacted private key]", value, flags=re.DOTALL)
    clipped = len(value) > 100_000
    value = value[:100_000]
    if multiline and re.search(r"</?(?:p|div|br|li|ol|ul|pre|span|script|style|head|h[1-6]|table)\b", value, re.I):
        parser = _DescriptionParser()
        parser.feed(value)
        parser.close()
        value = "".join(parser.parts)
    # Entities may have introduced controls or split up credential markers.
    value = public_text(value, max(len(value), 1))
    if multiline:
        value = re.sub(r"\n{3,}", "\n\n", value).strip()
    else:
        value = " ".join(value.split())
    if len(value) > maximum or clipped:
        value = value[:maximum].rstrip() + "\n…（内容过长，已截断）"
    return escape(value)
