"""HTML compared as a browser reads it, not as a template engine happens to write it.

A PHP template and its JavaScript port rarely agree on indentation, attribute order or `&#039;` versus `&#39;`.
`lines(html)` turns a page into one line per tag or text: tag and attribute names lower case, attributes sorted,
entities decoded and escaped one way, whitespace runs collapsed, whitespace between tags and comments dropped.
`<pre>` and `<textarea>` keep their text as it is. Everything else (tags, attribute values, text) must match.
"""

import re
import sys
from html import escape
from html.parser import HTMLParser

if sys.version_info >= (3, 12):
    from typing import override
else:
    from typing_extensions import override

SPACE = re.compile(r"[ \t\n\r\f]+")  # HTML's whitespace; a no-break space is text


def _collapse(text: str) -> str:
    return SPACE.sub(" ", text).strip(" ")


VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}
KEEP = {"pre", "textarea"}  # whitespace is content here
RAW = {"script", "style"}  # text is code: whitespace collapsed, never escaped


def _tag(tag: str, attrs: list[tuple[str, str | None]]) -> str:
    parts: list[str] = []
    for name, raw in sorted(attrs, key=lambda a: a[0]):
        value = _collapse(raw or "") if name == "class" else raw or ""
        parts.append(f' {name}="{escape(value)}"')
    return f"<{tag}{''.join(parts)}>"


class _Lines(HTMLParser):
    def __init__(self) -> None:
        """Parse with entities decoded."""
        super().__init__(convert_charrefs=True)
        self.lines: list[str] = []
        self.text: list[str] = []  # the text since the last tag (the parser may hand it over in pieces)
        self.keep = 0
        self.raw = 0

    def flush(self) -> None:
        """Add the text since the last tag as a line."""
        data, self.text = "".join(self.text), []
        if self.keep:
            if data:
                self.lines.append(escape(data.removeprefix("\n"), quote=False))  # a browser drops that newline
            return
        data = _collapse(data)
        if data:
            self.lines.append(data if self.raw else escape(data, quote=False))

    @override
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.flush()
        self.lines.append(_tag(tag, attrs))
        if tag in KEEP:
            self.keep += 1
        if tag in RAW:
            self.raw += 1

    @override
    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:  # <br/> is <br>
        self.flush()
        self.lines.append(_tag(tag, attrs))

    @override
    def handle_endtag(self, tag: str) -> None:
        self.flush()
        if tag in VOID:  # </br> and </input> mean nothing
            return
        self.lines.append(f"</{tag}>")
        if tag in KEEP and self.keep:
            self.keep -= 1
        if tag in RAW and self.raw:
            self.raw -= 1

    @override
    def handle_data(self, data: str) -> None:
        self.text.append(data)

    @override
    def handle_decl(self, decl: str) -> None:
        self.flush()
        self.lines.append(f"<!{_collapse(decl).lower()}>")

    @override
    def handle_pi(self, data: str) -> None:
        self.flush()
        self.lines.append(f"<?{data}>")

    @override
    def handle_comment(self, data: str) -> None:
        """Drop the comment; the text around it is one text."""


def lines(html: str) -> list[str]:
    """Return a page as its normalised lines (see the module docstring)."""
    parser = _Lines()
    parser.feed(html)
    parser.close()
    parser.flush()
    return parser.lines
