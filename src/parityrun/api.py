"""A library's API, read from its code: what `fn` and `new` steps can call, so `parityrun coverage` tells which parts
no scenario calls yet and `init` can list them in the first scenario. PHP for now: functions (namespaced too), public
static and instance methods, and the classes `new` can make.

Every part is a key, as golden/endpoints.json keeps it: "fn Cart\\slug", "fn Cart\\Cart::fromJson" (static),
"fn Cart\\Cart->add" (on an object), "new Cart\\Cart". `key(step, objects)` tells which part a step calls.
"""

from __future__ import annotations

import dataclasses
import pathlib
import re

from .routes import SKIP_DIRS

# strings, heredocs and comments, blanked out before the code is read (a brace in a string isn't one)
NOISE = re.compile(r"""<<<\s*['"]?(\w+)['"]?\r?\n.*?\n\s*\1\b|'(?:[^'\\]|\\.)*'|"(?:[^"\\]|\\.)*"|/\*.*?\*/|"""
                   r"""(?://|#(?!\[))[^\n]*""", re.S)
TOKEN = re.compile(r"\bnamespace\s+([\w\\]+)\s*(?=[;{])|"
                   r"\b((?:(?:abstract|final|readonly)\s+)*)(class|interface|trait|enum)\s+(\w+)|"
                   r"\bfunction\s+&?\s*(\w+)\s*\(|[{}]")
NOT_PUBLIC = re.compile(r"\b(?:private|protected)\b")


@dataclasses.dataclass(frozen=True)
class Part:
    key: str
    params: str
    """The parameters as the code writes them: "(string $text, string $separator = '-')"."""
    required: int
    """How many arguments a call must give."""
    source: str
    """Where it is: "src/functions.php:12"."""


def _blank(text: str) -> str:
    return NOISE.sub(lambda m: re.sub(r"[^\n]", " ", m.group(0)), text)


def _params(text: str, code: str, start: int) -> tuple[str, int]:
    """The parameter list that opens at `start` (a "("): as written, and how many have no default."""
    depth, end = 0, start
    for end in range(start, len(code)):
        depth += {"(": 1, ")": -1}.get(code[end], 0)
        if depth == 0:
            break
    inner = code[start + 1:end]
    parts, depth, last = [], 0, 0
    for i, ch in enumerate(inner):  # split at the commas between parameters, not those in a default's brackets
        depth += {"(": 1, "[": 1, ")": -1, "]": -1}.get(ch, 0)
        if ch == "," and depth == 0:
            parts.append(inner[last:i])
            last = i + 1
    parts.append(inner[last:])
    required = sum(1 for p in parts if p.strip() and "=" not in p and "..." not in p)
    return " ".join(text[start:end + 1].split()), required


def _modifiers(code: str, at: int) -> str:
    """The words before a declaration, back to the end of the statement or block before it."""
    start = max(code.rfind(c, 0, at) for c in ";{}") + 1
    return code[start:at]


def php(text: str, rel: str) -> list[Part]:
    """The API one PHP file declares."""
    code = _blank(text)
    parts: list[Part] = []
    namespace, ns_depth, depth = "", 0, 0
    classes: list[tuple[str, int, str]] = []  # (qualified name, the depth of its body, class/interface/trait/enum)
    pending: tuple[str, str] | None = None  # a class declared, its body not opened yet: (name, kind)
    constructors: dict[str, Part | None] = {}
    for m in TOKEN.finditer(code):
        token = m.group(0)
        line = code.count("\n", 0, m.start()) + 1
        if m.group(1):
            namespace = m.group(1).strip("\\")
            ns_depth = depth + (1 if code[m.end():m.end() + 1] == "{" else 0)
        elif m.group(3):
            if re.search(r"(?:\bnew|::)\s*$", code[max(0, m.start() - 20):m.start()]):
                continue  # `new class extends X {`, `Foo::class`: no class of the API
            name = f"{namespace}\\{m.group(4)}" if namespace else m.group(4)
            pending = (name, m.group(3))
            if m.group(3) == "class" and "abstract" not in m.group(2):
                constructors[name] = Part(f"new {name}", "()", 0, f"{rel}:{line}")
        elif m.group(5):
            name = m.group(5)
            params, required = _params(text, code, code.index("(", m.end() - 1))
            words = _modifiers(code, m.start())
            if classes and depth == classes[-1][1]:  # a method (an interface's or trait's is called on a class)
                cls, _, kind = classes[-1]
                if kind in ("interface", "trait"):
                    continue
                if name == "__construct":
                    if cls in constructors:
                        constructors[cls] = None if NOT_PUBLIC.search(words) else \
                            Part(f"new {cls}", params, required, f"{rel}:{line}")
                elif not NOT_PUBLIC.search(words) and not name.startswith("__"):
                    sep = "::" if re.search(r"\bstatic\b", words) else "->"
                    parts.append(Part(f"fn {cls}{sep}{name}", params, required, f"{rel}:{line}"))
            elif not classes and depth == ns_depth:  # a function of its own
                parts.append(Part(f"fn {namespace}\\{name}" if namespace else f"fn {name}", params, required,
                                  f"{rel}:{line}"))
        elif token == "{":
            depth += 1
            if pending:
                classes.append((pending[0], depth, pending[1]))
                pending = None
        elif token == "}":
            if classes and classes[-1][1] == depth:
                classes.pop()
            depth -= 1
    return [c for c in constructors.values() if c] + parts


def scan(root: pathlib.Path) -> tuple[list[Part], list[str]]:
    """The API of the PHP library in `root` (not vendor/, tests/ and the like), and notes on what wasn't found."""
    found: list[Part] = []
    for path in sorted(root.rglob("*.php")):
        rel = path.relative_to(root).as_posix()
        if path.is_file() and not set(rel.split("/")[:-1]) & SKIP_DIRS:
            found += php(path.read_text(encoding="utf-8", errors="replace"), rel)
    unique = list({p.key: p for p in found}.values())
    notes = [] if unique else [f"No PHP functions or classes found in {root}: list the API in golden/endpoints.json "
                               "yourself (\"fn name\", \"new Class\")."]
    return unique, notes


def key(step: dict, objects: dict[str, str]) -> str:
    """The API part a `fn` or `new` step calls; `objects` keeps which class each `as` name holds."""
    if "new" in step:
        if step.get("as"):
            objects[step["as"]] = step["new"]
        return f"new {step['new']}"
    m = re.fullmatch(r"([A-Za-z_]\w*)\.(\w+)", step["fn"])
    if m and m.group(1) in objects:
        return f"fn {objects[m.group(1)]}->{m.group(2)}"
    return f"fn {step['fn']}"
