"""Make two runs comparable: ids, times and today's date become placeholders; everything else must match exactly.

Ids are numbered by first appearance over a sorted-key walk of the whole recording (responses first, then the
database and files), so the same scenario gives the same numbers in every implementation.
"""

from __future__ import annotations

import datetime as dt
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .jsontypes import JSON

ID = re.compile(r"^(?:[0-9a-f]{8}|[0-9a-f]{12}|[0-9a-f]{32})$")
TIME = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?")
EPOCH = re.compile(r"^1[7-9]\d{8}(?:\.\d+)?$")  # unix seconds, 2023-2033
STATE = re.compile(r"(state=)[A-Za-z0-9_-]{16,}")  # a random OAuth state in a sign-in address
SALTED = re.compile(r"(pbkdf2_sha256\$\d+)\$[0-9a-f]+\$[0-9a-f]+")  # random salt: compare the algorithm and rounds


def _longest_first(pairs: list[tuple[str, str]]) -> list[tuple[str, str]]:
    return sorted(pairs, key=lambda kv: -len(kv[0]))


class Normalizer:
    """Replaces the values that differ from run to run by placeholders."""

    def __init__(self, literals: dict[str, str] | None = None) -> None:
        """Start with this run's own values (its data folder, the stub's port) and today's date."""
        super().__init__()
        self.ids: dict[str, str] = {}
        # values of this run only (its data folder, the stub's port), longest first
        self.literals = _longest_first(list((literals or {}).items()))
        today = dt.datetime.now().astimezone().date()
        self.dates = {
            today.isoformat(): "<date>",
            today.strftime("%Y-%m"): "<month>",
            (today - dt.timedelta(days=1)).isoformat(): "<date>",
        }

    def add(self, literals: dict[str, str]) -> None:
        """Replace more values of this run only (e.g. a captured CSRF token) by their labels."""
        self.literals = _longest_first([*self.literals, *literals.items()])

    def collect(self, value: JSON) -> None:
        """Give each id in `value` its number, in the order a sorted-key walk meets them."""
        if isinstance(value, dict):
            for key in sorted(value):
                self.collect(key)
                self.collect(value[key])
        elif isinstance(value, list):
            for item in value:
                self.collect(item)
        elif isinstance(value, str) and ID.match(value) and value not in self.ids:
            self.ids[value] = f"<id:{len(self.ids) + 1}>"

    def text(self, value: str) -> str:
        """Return `value` with this run's values, ids, times and dates replaced."""
        if value in self.ids:
            return self.ids[value]
        for raw, label in self.literals:
            value = value.replace(raw, label)
        value = SALTED.sub(r"\1$<salt>$<hash>", value)
        value = STATE.sub(r"\1<state>", value)
        value = TIME.sub("<time>", value)
        for raw, label in self.ids.items():
            if raw in value:
                value = value.replace(raw, label)
        for raw, label in self.dates.items():
            value = value.replace(raw, label)
        return value

    def apply(self, value: JSON, key: str = "") -> JSON:
        """Return `value` normalised: every string and key by `text`, unix times as "<epoch>"."""
        if isinstance(value, dict):
            return {self.text(k): self.apply(v, k) for k, v in value.items()}
        if isinstance(value, list):
            return [self.apply(v, key) for v in value]
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and EPOCH.match(str(value)):
            return "<epoch>"
        return value
