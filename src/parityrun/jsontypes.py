"""The types of the data parityrun passes around: JSON values (recordings, requests, answers) and YAML mappings."""

from __future__ import annotations

from typing import Any, TypeAlias

JSON: TypeAlias = "dict[str, JSON] | list[JSON] | str | int | float | bool | None"
"""Any JSON value, walked as a whole (normalised, compared, saved)."""

Data: TypeAlias = "dict[str, Any]"
"""A JSON object or YAML mapping that code reads key by key: a recording, a scenario, a step, a request. Its shape
is the data's (the wiki's Scenarios page), not Python's, so its values are checked where they are used."""
