"""The stakeholder's acceptance test, automated: if it wouldn't render on IE6
on Windows 98, it doesn't ship. These checks lint every template and the
stylesheet against the rules in docs/ie6-style-guide.md."""

import html.entities
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


# Characters above ASCII that Windows 98 can actually draw. Its shipped fonts
# (Tahoma, Arial, Times New Roman) cover Latin-1 in full plus the WGL4 extras
# listed here; anything else renders as a hollow box on the stakeholder's
# machine. Emoji are the case that matters in practice — U+1F338 CHERRY BLOSSOM
# was a natural fit for a thing called SakuraFinancial and sat in the title bar
# of every page until someone tried it on the real hardware. It postdates
# Windows 98 by twelve years. Add to this set only after checking the glyph
# exists in a Win98 core font.
WGL4_EXTRAS = {
    0x2013, 0x2014,                              # en/em dash
    0x2018, 0x2019, 0x201C, 0x201D,              # curly quotes
    0x2022, 0x2026, 0x2030, 0x2039, 0x203A,      # bullet, ellipsis, permille, guillemets
    0x20AC, 0x2122,                              # euro, trademark
    0x2190, 0x2191, 0x2192, 0x2193, 0x2194, 0x2195,   # arrows
    0x2212, 0x221A, 0x221E, 0x2260, 0x2264, 0x2265,   # math
    0x25A0, 0x25AA, 0x25B2, 0x25BA, 0x25BC, 0x25C4, 0x25CA, 0x25CF,  # geometric
    0x2660, 0x2663, 0x2665, 0x2666,              # card suits
}

ENTITY_RE = re.compile(r"&#x([0-9A-Fa-f]+);|&#([0-9]+);|&([A-Za-z][A-Za-z0-9]*);")


def _win98_can_draw(codepoint: int) -> bool:
    return codepoint < 0x80 or 0xA0 <= codepoint <= 0xFF or codepoint in WGL4_EXTRAS


def _drawn_characters(text: str):
    """Yield (codepoint, source) for everything the browser will paint, with
    numeric and named entities resolved to the glyph they actually produce —
    an emoji written as &#127800; is still an emoji on screen."""
    for match in ENTITY_RE.finditer(text):
        as_hex, as_decimal, name = match.groups()
        if as_hex is not None:
            yield int(as_hex, 16), match.group(0)
        elif as_decimal is not None:
            yield int(as_decimal), match.group(0)
        else:
            resolved = html.entities.html5.get(name + ";")
            if resolved and len(resolved) == 1:
                yield ord(resolved), match.group(0)
    for char in text:
        if ord(char) > 0x7F:
            yield ord(char), char


def test_templates_stay_inside_the_windows98_character_set():
    for path in TEMPLATES:
        for codepoint, source in _drawn_characters(path.read_text()):
            assert _win98_can_draw(codepoint), (
                f"{path.name}: {source!r} is U+{codepoint:04X} "
                f"({unicodedata_name(codepoint)}), which no Windows 98 font can draw — "
                "it renders as a box in IE6"
            )


def test_stylesheet_stays_inside_the_windows98_character_set():
    text = re.sub(r"/\*.*?\*/", "", CSS_FILE.read_text(), flags=re.DOTALL)
    for codepoint, source in _drawn_characters(text):
        assert _win98_can_draw(codepoint), (
            f"sakura.css: {source!r} is U+{codepoint:04X} "
            f"({unicodedata_name(codepoint)}), which no Windows 98 font can draw"
        )


def unicodedata_name(codepoint: int) -> str:
    import unicodedata

    return unicodedata.name(chr(codepoint), "unnamed")


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
