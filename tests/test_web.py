"""Web sites: forms, uploads, values captured from pages, redirects and HTML compared as a browser reads it."""

from __future__ import annotations

import pathlib
import urllib.parse
from typing import TYPE_CHECKING, cast

import httpx
import pytest

from parityrun import Project, runner
from parityrun.htmlnorm import lines
from parityrun.normalize import Normalizer

if TYPE_CHECKING:
    from parityrun.jsontypes import Data


def test_html_ignores_how_a_template_wrote_it() -> None:
    php = (
        '<!DOCTYPE html>\n<html lang="en">\n  <body>\n    <!-- list -->\n'
        '    <a title="it&#039;s" href="/x?a=1&amp;b=2" class="btn  big">Tom &amp; Jerry</a>\n'
        '    <br/>\n    <input type="hidden" name="csrf" value="t">\n  </body>\n</html>\n'
    )
    js = (
        '<!doctype html><html lang="en"><body><a href="/x?a=1&b=2" class="btn big" title="it&#39;s">'
        'Tom &amp;   Jerry</a><br><input name="csrf" value="t" type="hidden"></input></body></html>'
    )
    assert (
        lines(php)
        == lines(js)
        == [
            "<!doctype html>",
            '<html lang="en">',
            "<body>",
            '<a class="btn big" href="/x?a=1&amp;b=2" title="it&#x27;s">',
            "Tom &amp; Jerry",
            "</a>",
            "<br>",
            '<input name="csrf" type="hidden" value="t">',
            "</body>",
            "</html>",
        ]
    )


def test_html_still_tells_real_differences() -> None:
    assert lines("<p>Hello</p>") != lines("<p>Hello!</p>")
    assert lines('<a href="/a">x</a>') != lines('<a href="/b">x</a>')
    assert lines("<p>x</p>") != lines("<div>x</div>")
    assert lines("<input checked>") != lines("<input>")
    assert lines("<p>a <b>b</b></p>") == ["<p>", "a", "<b>", "b", "</b>", "</p>"]
    assert lines("<p>\u00a0</p>") == ["<p>", "\u00a0", "</p>"]  # a no-break space is text, not layout


def test_html_keeps_whitespace_where_it_is_content() -> None:
    assert lines("<pre>\n  a\n    b</pre>") == ["<pre>", "  a\n    b", "</pre>"]
    assert lines("<textarea>x  y</textarea>") == ["<textarea>", "x  y", "</textarea>"]
    assert lines("<script>if (a < b) {\n  go();\n}</script>") == ["<script>", "if (a < b) { go(); }", "</script>"]


def _client(seen: list[httpx.Request]) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/go":
            return httpx.Response(303, headers={"Location": "/there"})
        return httpx.Response(200, text="ok")

    return httpx.Client(base_url="http://svc", transport=httpx.MockTransport(handler))


def test_form_steps_send_what_a_browser_sends(tmp_path: pathlib.Path) -> None:
    seen: list[httpx.Request] = []
    client = _client(seen)
    step: Data = {
        "call": "POST /sign",
        "form": {"name": "{{who}}", "remember": True, "spam": False, "n": 3, "tags[]": ["a", "b"], "empty": None},
    }
    _ = runner._request(client, step, {"who": "Åsa & co"}, tmp_path)
    request = seen[-1]
    assert request.headers["content-type"] == "application/x-www-form-urlencoded"
    assert urllib.parse.parse_qsl(request.content.decode(), keep_blank_values=True) == [
        ("name", "Åsa & co"),
        ("remember", "on"),
        ("n", "3"),
        ("tags[]", "a"),
        ("tags[]", "b"),
        ("empty", ""),
    ]


def test_uploads_are_multipart_with_the_fixture_file(tmp_path: pathlib.Path) -> None:
    seen: list[httpx.Request] = []
    _ = (tmp_path / "cat.png").write_bytes(b"\x89PNG fake")
    _ = runner._request(
        _client(seen), {"call": "POST /up", "form": {"title": "Cat"}, "upload": {"photo": "cat.png"}}, {}, tmp_path
    )
    request = seen[-1]
    assert request.headers["content-type"].startswith("multipart/form-data; boundary=")
    body = request.read()
    assert b'name="title"\r\n\r\nCat' in body
    assert b'name="photo"; filename="cat.png"' in body
    assert b"\x89PNG fake" in body


def test_follow_goes_to_the_page_a_redirect_points_at(tmp_path: pathlib.Path) -> None:
    seen: list[httpx.Request] = []
    client = _client(seen)
    assert runner._request(client, {"call": "POST /go"}, {}, tmp_path).status_code == 303
    r = runner._request(client, {"call": "POST /go", "follow": True}, {}, tmp_path)
    assert r.status_code == 200
    assert [h.status_code for h in r.history] == [303]
    assert [x.url.path for x in seen] == ["/go", "/go", "/there"]


def test_captures_find_values_in_pages_and_headers() -> None:
    page = httpx.Response(
        302,
        headers={"Location": "/entries/ab12cd34", "Content-Type": "text/html"},
        text='<input name="csrf" value="a&amp;b"><h1>Entry 7</h1>',
    )
    found = runner._capture(
        page,
        {
            "csrf": 'name="csrf" value="([^"]+)"',
            "heading": "Entry \\d+",
            "entry": {"header": "location", "regex": "/entries/(\\w+)"},
        },
        "text/html",
        "s",
    )
    assert found == {"csrf": "a&b", "heading": "Entry 7", "entry": "ab12cd34"}
    with pytest.raises(RuntimeError, match=r"capture missing found nothing"):
        _ = runner._capture(page, {"missing": "nope (\\d+)"}, "text/html", "s")


def test_captured_tokens_get_labels_but_ids_keep_their_numbers() -> None:
    norm = Normalizer()
    raw: Data = {
        "files": {"e.json": {"json": {"id": "ab12cd34"}}},
        "steps": [{"html": ['<input value="Zx9-long-token">', '<a href="/entries/ab12cd34">']}],
    }
    norm.collect(raw)
    captured = {"Zx9-long-token": "<csrf>", "ab12cd34": "<entry>"}
    norm.add({v: label for v, label in captured.items() if v not in norm.ids})
    assert cast("Data", norm.apply(raw))["steps"][0]["html"] == ['<input value="<csrf>">', '<a href="/entries/<id:1>">']


def test_new_parts_are_compared_and_named(tmp_path: pathlib.Path) -> None:
    golden: Data = {
        "steps": [
            {
                "call": "POST /sign",
                "status": 303,
                "location": "/entries/<id:1>",
                "redirects": [],
                "html": ["<p>", "a", "</p>"],
            }
        ],
        "db": {},
        "files": {},
    }
    got: Data = {
        "steps": [
            {"call": "POST /sign", "status": 303, "location": "/", "redirects": [], "html": ["<p>", "b", "</p>"]}
        ],
        "db": {},
        "files": {},
    }
    results = {c["name"]: c for c in runner.checks(golden, got, tmp_path, tmp_path)}
    assert list(results) == [
        "step 0 POST /sign status",
        "step 0 POST /sign location",
        "step 0 POST /sign redirects",
        "step 0 POST /sign html",
    ]
    assert not results["step 0 POST /sign location"]["ok"]
    assert results["step 0 POST /sign redirects"]["ok"]
    assert results["step 0 POST /sign html"]["detail"] == ["[1]: 'a' != 'b'"]


def test_web_settings_are_off_by_default(tmp_path: pathlib.Path) -> None:
    project = Project(root=tmp_path)
    assert (project.html, project.redirects, project.snapshot, project.setup) == (False, False, None, None)
    assert pathlib.Path(project.root) == tmp_path.resolve()
