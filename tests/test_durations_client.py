"""The dialog's duration check (static/app.js, static/index.html) must agree with
the server's (app/core/durations.py): the same pattern and the same seconds per
unit. A mismatch would let the dialog accept what the server refuses, or the
other way round. There is no JavaScript test runner here, so this reads the
constants from the files."""
import pathlib
import re

from app.core import durations

STATIC = pathlib.Path(__file__).resolve().parent.parent / "static"


def _js_const(name: str) -> str:
    source = (STATIC / "app.js").read_text()
    match = re.search(rf"^var {name} = ([^;]+);", source, re.MULTILINE)
    assert match, f"var {name} not found in static/app.js"
    return match.group(1)


def test_the_dialogs_pattern_is_the_servers():
    js = _js_const("DURATION_RE")
    assert js.startswith("/") and js.endswith("/"), js
    assert js[1:-1] == durations.DURATION_RE.pattern


def test_the_input_pattern_is_the_servers():
    html = (STATIC / "index.html").read_text()
    match = re.search(r'id="key-duration"[^>]*pattern="([^"]+)"', html)
    assert match, "the duration input's pattern not found"
    # HTML patterns are anchored implicitly.
    assert "^" + match.group(1) + "$" == durations.DURATION_RE.pattern


def test_the_dialogs_units_are_the_servers():
    js = _js_const("UNIT_SECONDS")
    units = {}
    for unit, expr in re.findall(r"(\w+):\s*([0-9 *]+)", js):
        assert re.fullmatch(r"[0-9 *]+", expr)
        value = 1
        for factor in expr.split("*"):
            value *= int(factor)
        units[unit] = value
    assert units == durations.UNIT_SECONDS


def test_the_dialogs_longest_duration_is_the_servers():
    js = _js_const("MAX_DURATION_SECONDS").strip()
    assert re.fullmatch(r"[0-9 *]+", js), js
    value = 1
    for factor in js.split("*"):
        value *= int(factor)
    assert value == durations.MAX_SECONDS
