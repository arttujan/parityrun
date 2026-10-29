"""HTML compared as a browser reads it, not as a template engine happens to write it.

A PHP template and its JavaScript port rarely agree on indentation, attribute order or `&#039;` versus `&#39;`.
`lines(html)` turns a page into one line per tag or text: tag and attribute names lower case, attributes sorted,
entities decoded and escaped one way, whitespace runs collapsed, whitespace between tags and comments dropped.
`<pre>` and `<textarea>` keep their text as it is. Everything else (tags, attribute values, text) must match.
"""

import re
from html import escape
from html.parser import HTMLParser

SPACE = re.compile(r"[ \t\n\r\f]+")  # HTML's whitespace; a no-break space is text


def _collapse(text: str) -> str:
    return SPACE.sub(" ", text).strip(" ")


VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}
KEEP = {"pre", "textarea"}  # whitespace is content here
RAW = {"script", "style"}  # text is code: whitespace collapsed, never escaped


def _tag(tag: str, attrs: list[tuple[str, str | None]]) -> str:
    parts = []
    for name, value in sorted(attrs, key=lambda a: a[0]):
        value = value or ""
        if name == "class":
            value = _collapse(value)
        parts.append(f' {name}="{escape(value)}"')
    return f"<{tag}{''.join(parts)}>"


class _Lines(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.lines: list[str] = []
        self.text: list[str] = []  # the text since the last tag (the parser may hand it over in pieces)
        self.keep = 0
        self.raw = 0

    def flush(self) -> None:
        data, self.text = "".join(self.text), []
        if self.keep:
            if data:
                self.lines.append(escape(data.removeprefix("\n"), quote=False))  # a browser drops that newline
            return
        data = _collapse(data)
        if data:
            self.lines.append(data if self.raw else escape(data, quote=False))

    def handle_starttag(self, tag, attrs):
        self.flush()
        self.lines.append(_tag(tag, attrs))
        if tag in KEEP:
            self.keep += 1
        if tag in RAW:
            self.raw += 1

    def handle_startendtag(self, tag, attrs):  # <br/> is <br>
        self.flush()
        self.lines.append(_tag(tag, attrs))

    def handle_endtag(self, tag):
        self.flush()
        if tag in VOID:  # </br> and </input> mean nothing
            return
        self.lines.append(f"</{tag}>")
        if tag in KEEP and self.keep:
            self.keep -= 1
        if tag in RAW and self.raw:
            self.raw -= 1

    def handle_data(self, data):
        self.text.append(data)

    def handle_decl(self, decl):
        self.flush()
        self.lines.append(f"<!{_collapse(decl).lower()}>")

    def handle_pi(self, data):
        self.flush()
        self.lines.append(f"<?{data}>")

    def handle_comment(self, data):  # dropped; the text around it is one text
        pass


def lines(html: str) -> list[str]:
    """A page as its normalised lines (see the module docstring)."""
    parser = _Lines()
    parser.feed(html)
    parser.close()
    parser.flush()
    return parser.lines
