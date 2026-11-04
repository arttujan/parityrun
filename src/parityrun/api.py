r"""A library's API, read from its code: what `fn` and `new` steps can call.

`parityrun coverage` then tells which parts no scenario calls yet, and `init` can list them in the first scenario.
PHP for now: functions (namespaced too), public static and instance methods, and the classes `new` can make.

Every part is a key, as golden/endpoints.json keeps it: "fn Cart\\slug", "fn Cart\\Cart::fromJson" (static),
"fn Cart\\Cart->add" (on an object), "new Cart\\Cart". `key(step, objects)` tells which part a step calls.
"""

from __future__ import annotations

import dataclasses
import re
from typing import TYPE_CHECKING

from .routes import SKIP_DIRS

if TYPE_CHECKING:
    import pathlib

    from .jsontypes import Data

# strings, heredocs and comments, blanked out before the code is read (a brace in a string isn't one)
NOISE = re.compile(
    r"""<<<\s*['"]?(\w+)['"]?\r?\n.*?\n\s*\1\b|'(?:[^'\\]|\\.)*'|"(?:[^"\\]|\\.)*"|/\*.*?\*/|"""
    r"""(?://|#(?!\[))[^\n]*""",
    re.DOTALL,
)
TOKEN = re.compile(
    r"\bnamespace\s+([\w\\]+)\s*(?=[;{])|"
    r"\b((?:(?:abstract|final|readonly)\s+)*)(class|interface|trait|enum)\s+(\w+)|"
    r"\bfunction\s+&?\s*(\w+)\s*\(|[{}]"
)
NOT_PUBLIC = re.compile(r"\b(?:private|protected)\b")


@dataclasses.dataclass(frozen=True)
class Part:
    """One part of a library's API: a function, a method or a class `new` can make."""

    key: str
    """The part as golden/endpoints.json keeps it: "fn Cart\\slug", "new Cart\\Cart"."""
    params: str
    """The parameters as the code writes them: "(string $text, string $separator = '-')"."""
    required: int
    """How many arguments a call must give."""
    source: str
    """Where it is: "src/functions.php:12"."""


def _blank(text: str) -> str:
    return NOISE.sub(lambda m: re.sub(r"[^\n]", " ", m.group(0)), text)


def _params(text: str, code: str, start: int) -> tuple[str, int]:
    """Return the parameter list that opens at `start` (a "("): as written, and how many have no default."""
    depth, end = 0, start
    for end in range(start, len(code)):
        depth += {"(": 1, ")": -1}.get(code[end], 0)
        if depth == 0:
            break
    inner = code[start + 1 : end]
    parts: list[str] = []
    depth, last = 0, 0
    for i, ch in enumerate(inner):  # split at the commas between parameters, not those in a default's brackets
        depth += {"(": 1, "[": 1, ")": -1, "]": -1}.get(ch, 0)
        if ch == "," and depth == 0:
            parts.append(inner[last:i])
            last = i + 1
    parts.append(inner[last:])
    required = sum(1 for p in parts if p.strip() and "=" not in p and "..." not in p)
    return " ".join(text[start : end + 1].split()), required


def _modifiers(code: str, at: int) -> str:
    """Return the words before a declaration, back to the end of the statement or block before it."""
    start = max(code.rfind(c, 0, at) for c in ";{}") + 1
    return code[start:at]


@dataclasses.dataclass
class _Reader:
    """Reads one PHP file's declarations in order, keeping track of the namespace, classes and brace depth."""

    text: str
    rel: str
    code: str = ""
    parts: list[Part] = dataclasses.field(default_factory=list[Part])
    namespace: str = ""
    ns_depth: int = 0
    depth: int = 0
    classes: list[tuple[str, int, str]] = dataclasses.field(default_factory=list[tuple[str, int, str]])
    """(qualified name, the depth of its body, class/interface/trait/enum)"""
    pending: tuple[str, str] | None = None
    """A class declared, its body not opened yet: (name, kind)."""
    constructors: dict[str, Part | None] = dataclasses.field(default_factory=dict[str, "Part | None"])

    def read(self) -> list[Part]:
        self.code = _blank(self.text)
        for m in TOKEN.finditer(self.code):
            symbol = m.group(0)
            if m.group(1):
                self.namespace = m.group(1).strip("\\")
                self.ns_depth = self.depth + (1 if self.code[m.end() : m.end() + 1] == "{" else 0)
            elif m.group(3):
                self._declaration(m)
            elif m.group(5):
                self._function(m)
            elif symbol == "{":
                self.depth += 1
                if self.pending:
                    self.classes.append((self.pending[0], self.depth, self.pending[1]))
                    self.pending = None
            elif symbol == "}":
                if self.classes and self.classes[-1][1] == self.depth:
                    _ = self.classes.pop()
                self.depth -= 1
        return [c for c in self.constructors.values() if c] + self.parts

    def _where(self, m: re.Match[str]) -> str:
        return f"{self.rel}:{self.code.count(chr(10), 0, m.start()) + 1}"

    def _declaration(self, m: re.Match[str]) -> None:
        if re.search(r"(?:\bnew|::)\s*$", self.code[max(0, m.start() - 20) : m.start()]):
            return  # `new class extends X {`, `Foo::class`: no class of the API
        name = f"{self.namespace}\\{m.group(4)}" if self.namespace else m.group(4)
        self.pending = (name, m.group(3))
        if m.group(3) == "class" and "abstract" not in m.group(2):
            self.constructors[name] = Part(f"new {name}", "()", 0, self._where(m))

    def _function(self, m: re.Match[str]) -> None:
        name = m.group(5)
        params, required = _params(self.text, self.code, self.code.index("(", m.end() - 1))
        words = _modifiers(self.code, m.start())
        if self.classes and self.depth == self.classes[-1][1]:  # a method (an interface's or trait's is on a class)
            cls, _, kind = self.classes[-1]
            if kind in ("interface", "trait"):
                return
            if name == "__construct":
                if cls in self.constructors:
                    public = not NOT_PUBLIC.search(words)
                    self.constructors[cls] = Part(f"new {cls}", params, required, self._where(m)) if public else None
            elif not NOT_PUBLIC.search(words) and not name.startswith("__"):
                sep = "::" if re.search(r"\bstatic\b", words) else "->"
                self.parts.append(Part(f"fn {cls}{sep}{name}", params, required, self._where(m)))
        elif not self.classes and self.depth == self.ns_depth:  # a function of its own
            key = f"fn {self.namespace}\\{name}" if self.namespace else f"fn {name}"
            self.parts.append(Part(key, params, required, self._where(m)))


def php(text: str, rel: str) -> list[Part]:
    """Return the API one PHP file declares."""
    return _Reader(text, rel).read()


def scan(root: pathlib.Path) -> tuple[list[Part], list[str]]:
    """Return the API of the PHP library in `root` (not vendor/, tests/ and so on), and notes on what's missing."""
    found: list[Part] = []
    for path in sorted(root.rglob("*.php"), key=lambda p: p.relative_to(root).as_posix()):  # one order on every OS
        rel = path.relative_to(root).as_posix()
        if path.is_file() and not set(rel.split("/")[:-1]) & SKIP_DIRS:
            found += php(path.read_text(encoding="utf-8", errors="replace"), rel)
    unique = list({p.key: p for p in found}.values())
    if unique:
        return unique, []
    note = f'No PHP functions or classes found in {root}: list the API in golden/endpoints.json yourself ("fn name", '
    return unique, [note + '"new Class").']


def key(step: Data, objects: dict[str, str]) -> str:
    """Return the API part a `fn` or `new` step calls; `objects` keeps which class each `as` name holds."""
    if "new" in step:
        if step.get("as"):
            objects[step["as"]] = step["new"]
        return f"new {step['new']}"
    m = re.fullmatch(r"([A-Za-z_]\w*)\.(\w+)", step["fn"])
    if m and m.group(1) in objects:
        return f"fn {objects[m.group(1)]}->{m.group(2)}"
    return f"fn {step['fn']}"
