# IE6 frontend rules & visual style

The stakeholder's acceptance test is literal: **if a page doesn't render in
Internet Explorer 6 on Windows 98, it doesn't ship.** These rules exist so
every template change stays inside that envelope, and they're enforced by the
template lint test in `services/web-ui/tests/test_ie6_lint.py`.

## Hard rules

1. Doctype is exactly `HTML 4.01 Transitional`.
2. Layout is `<table>`-based. No floats for layout, no flexbox, no grid, no
   `position: fixed`.
3. CSS stays inside the IE6-safe subset (see the allowlist in the lint test):
   basic box properties, `background-color`, `color`, `border`, `font-*`,
   `text-*`, `width/height`, `padding/margin`, `vertical-align`. No
   `:hover` on non-anchors, no attribute selectors, no `>` child selectors,
   no `max-width`/`min-width`, no media queries.
4. **No page may require JavaScript.** Every interaction is a form submit or a
   link. POST handlers redirect (POST → redirect → GET) so refresh is safe.
5. Images: GIF, JPEG, or non-alpha PNG (IE6 renders alpha PNGs gray).
6. Charts are server-rendered PNGs embedded with `<img>`.
7. Fonts: system stack only — `Tahoma, Verdana, Arial, sans-serif`
   (MS Sans Serif territory). No web fonts.
8. Served over plain HTTP on the LAN; IE6 has no modern TLS. Never expose the
   port beyond the LAN.
9. **No character Windows 98 cannot draw, and no emoji, ever.** Text is ASCII
   plus Latin-1 plus the handful of WGL4 symbols in the lint's allowlist —
   arrows, dashes, curly quotes, `»`, `±`, geometric shapes. Writing the
   character as an HTML entity does not help: `&#127800;` paints the same
   hollow box as a pasted 🌸, because Win98's fonts have no glyph for it.
   The lint resolves entities before checking, so both forms fail.

   Emoji is the trap worth naming: the whole set postdates Windows 98 by more
   than a decade. Where a modern UI reaches for an icon, use what the era had
   — `(!)` for warnings, `»` for an indented child row, `<->` or `&#8596;` for
   a transfer, `&#177;` for a value change.

## Visual style: period-correct, on purpose

The UI should feel like a well-kept Win98-era desktop app (think MS Money /
Quicken 2000), not a modern site squeezed into old HTML:

- Silver `#c0c0c0` application chrome; white `#ffffff` work surfaces.
- Navy `#000080` title bars and table header strips with white bold text.
- 3D bevels: `border: 2px outset` on raised panels/buttons, `2px inset` on
  sunken things (ledger tables, form fields).
- Chunky beveled buttons (`<input type=submit>` styled with outset borders and
  silver background).
- Ledger tables: alternating row colors (`#ffffff` / `#ece9d8`), sunken
  border, right-aligned amount columns, red negatives.
- Classic underlined blue links (`#0000ee`, visited `#551a8b`).
- Small type: 11px Tahoma/Verdana. Dense is fine; this is an accounting app.
- The odd `<hr>` divider is encouraged. Restraint on animated GIFs.

## Patterns for common interactions (no JS)

- **Payee auto-fill:** the entry form has a "Fill" submit button next to the
  payee `<select>`; it re-renders the form with the payee's default category
  and last amount filled in.
- **Split rows:** "Add split line" button re-renders the form with one more
  row.
- **Confirmations:** a small intermediate page with Yes/No forms.
- **Progress/updates:** full page reloads; a `<meta http-equiv=refresh>` is
  acceptable for long jobs.
