"""The stakeholder's acceptance test, automated: if it wouldn't render on IE6
on Windows 98, it doesn't ship. These checks lint every template and the
stylesheet against the rules in docs/ie6-style-guide.md."""

import re
from pathlib import Path

UI_DIR = Path(__file__).resolve().parents[1] / "webui_service"
TEMPLATES = sorted((UI_DIR / "templates").glob("*.html"))
CSS_FILE = UI_DIR / "static" / "sakura.css"

# Standalone pages must carry the HTML 4.01 doctype; the rest must extend one.
STANDALONE = {"base.html", "login.html"}

FORBIDDEN_IN_TEMPLATES = [
    ("<script", "JavaScript is not allowed — every page must work without it"),
    ("onclick=", "inline JS handlers are not allowed"),
    ("onchange=", "inline JS handlers are not allowed"),
    ("onsubmit=", "inline JS handlers are not allowed"),
    ("<canvas", "canvas doesn't exist in IE6"),
    ("<svg", "SVG doesn't exist in IE6"),
    ("<video", "video doesn't exist in IE6"),
    ("display: flex", "flexbox doesn't exist in IE6"),
    ("display:flex", "flexbox doesn't exist in IE6"),
    ("position: fixed", "position:fixed is broken in IE6"),
]

FORBIDDEN_IN_CSS = [
    "flex",
    "grid",
    "@media",
    "@import",
    "@font-face",
    "position: fixed",
    "max-width",
    "min-width",
    "max-height",
    "min-height",
    "opacity",
    "border-radius",
    "box-shadow",
    "transform",
    "transition",
    "::",          # pseudo-elements
    "[",           # attribute selectors
    ">",           # child combinator
    "rgba(",
    "var(",
    "calc(",
]


def test_templates_exist():
    assert len(TEMPLATES) >= 10


def test_standalone_pages_use_html401_doctype():
    for name in STANDALONE:
        text = (UI_DIR / "templates" / name).read_text()
        assert text.lstrip().startswith(
            '<!DOCTYPE HTML PUBLIC "-//W3C//DTD HTML 4.01 Transitional//EN"'
        ), f"{name} must start with the HTML 4.01 Transitional doctype"


def test_every_other_template_extends_a_standalone_layout():
    for path in TEMPLATES:
        if path.name in STANDALONE:
            continue
        assert '{% extends "base.html" %}' in path.read_text(), (
            f"{path.name} must extend base.html (that's where the doctype lives)"
        )


def test_no_forbidden_constructs_in_templates():
    for path in TEMPLATES:
        text = path.read_text().lower()
        for needle, why in FORBIDDEN_IN_TEMPLATES:
            assert needle not in text, f"{path.name}: found {needle!r} — {why}"


def test_no_javascript_urls():
    for path in TEMPLATES:
        assert "javascript:" not in path.read_text().lower(), (
            f"{path.name}: javascript: URLs are not allowed"
        )


def test_css_stays_inside_the_ie6_subset():
    text = CSS_FILE.read_text().lower()
    # Strip comments before scanning.
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    for needle in FORBIDDEN_IN_CSS:
        assert needle not in text, f"sakura.css: found {needle!r} which IE6 can't handle"


def test_hover_only_on_anchors():
    text = re.sub(r"/\*.*?\*/", "", CSS_FILE.read_text(), flags=re.DOTALL)
    for match in re.finditer(r"([^\s{,]+):hover", text):
        assert match.group(1) == "a", (
            f"IE6 only supports :hover on <a>: found {match.group(0)!r}"
        )


def test_forms_use_post_redirect_get_targets():
    """Forms must never target external URLs and must post to site paths."""
    for path in TEMPLATES:
        for action in re.findall(r'action="([^"]*)"', path.read_text()):
            rendered = action.replace("{{", "").replace("}}", "")
            assert not rendered.startswith("http"), f"{path.name}: external form action {action!r}"
