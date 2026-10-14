"""LiteLLM key durations ("30d", "1mo"): parsing, and comparing with MAX_KEY_DURATION.

Shared by the key routes and the startup check. static/app.js mirrors the
pattern and the comparison, for earlier feedback in the dialog; this module is
the one that counts.

A month is 30 days here. LiteLLM itself reads "mo" as calendar months (28 to 31
days, depending on the date), so the app sends LiteLLM months as days
(``for_litellm``): a key then lasts exactly the duration that was checked
against the maximum.
"""
from __future__ import annotations

import re

# A positive integer followed by a unit (per LiteLLM: s, m, h, d, mo). ASCII
# digits only ([0-9], not \d, which also matches e.g. Arabic-Indic digits), and
# at most 9 of them: 999,999,999 seconds is 31 years, and a longer string would
# only make int() fail (Python refuses integers over 4,300 digits), a 500
# instead of a 400.
DURATION_RE = re.compile(r"^([0-9]{1,9})(s|m|h|d|mo)$")

DAYS_PER_MONTH = 30
UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "mo": DAYS_PER_MONTH * 86400}


def parse(value: str) -> tuple[int, str] | None:
    """(count, unit) for a valid duration, or None (not one, or zero)."""
    match = DURATION_RE.fullmatch(value)
    if not match or int(match.group(1)) <= 0:
        return None
    return int(match.group(1)), match.group(2)


def seconds(value: str) -> int:
    """A valid duration in seconds, a month counting as 30 days."""
    n, unit = parse(value)
    return n * UNIT_SECONDS[unit]


def within(duration: str, limit: str) -> bool:
    """True if ``duration`` is no longer than ``limit`` (both valid)."""
    return seconds(duration) <= seconds(limit)


def for_litellm(value: str) -> str:
    """The duration to send LiteLLM: months as days ("3mo" -> "90d"), since
    LiteLLM would read them as calendar months; anything else as given."""
    n, unit = parse(value)
    return f"{n * DAYS_PER_MONTH}d" if unit == "mo" else value
