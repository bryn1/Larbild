# Lärbild design system

The single styling source for the Lärbild frontend. Everything (the shell, the
Bibliotek view, Skapa/Film funnels, every later view) consumes these tokens and
component classes — no view should hard-code colors, radii, shadows, or layout
dimensions.

Ported from the SFW design handoff (`/tmp/sfw_design/handoff/` on Sibbis:
`design/styles.css` + `docs/DESIGNSYSTEM.md`). Fidelity is **hi-fi** — colors,
typography, spacing and layout are final and recreated faithfully.

## Files & load order

| File | What it is |
|------|------------|
| `tokens.css` | **The single source of truth.** All design tokens on `:root` (color, type scale, spacing, radii, shadows, layout). |
| `base.css` | Reset, root element defaults, typography utilities (`.serif` `.mono` `.dim` `.faint` `.upper`), scrollbar. |
| `components.css` | All reusable component classes (shell, rail, buttons, chips, fields, panels, cards, tabs, matrix, timeline, …). |
| `larbild.css` | Entry point — `@import`s the three above in the correct order. |
| `../styleguide.html` | Visual spot-check: renders every token group + a component gallery against the handoff. |

Order matters: `tokens.css` → `base.css` → `components.css`. Use `larbild.css`
to get all three at once.

## Wiring it into a page

```html
<!-- 1. fonts (the three the system references) -->
<link rel="preconnect" href="https://fonts.googleapis.com"/>
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin/>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Fraunces:ital,opsz,wght@0,9..144,400;0,9..144,500;1,9..144,400&family=Schibsted+Grotesk:wght@400;500;600;700&family=Space+Mono:wght@400;700&display=swap"/>

<!-- 2. the design system -->
<link rel="stylesheet" href="/static/css/larbild.css"/>
```

Fonts: **Fraunces** (serif headings), **Schibsted Grotesk** (UI/body), **Space
Mono** (metadata only). The CSS falls back to system fonts if these are
unavailable, but the intended look needs them loaded. (Self-hosting the fonts
instead of the Google CDN is a reasonable later hardening step for this
LAN-only tool — out of scope here.)

## The subject-color mechanism (`--brand-h`)

There is **one accent per view**: the active subject's hue. A view sets the hue
once and the whole accent system retints:

```js
document.documentElement.style.setProperty("--brand-h", subject.hue); // e.g. 248 for Fysik
```

All `--brand*` tokens (`--brand`, `--brand-soft`, `--brand-line`, `--brand-ink`,
`--brand-ring`) derive from `--brand-h` in OKLCH. The default is **268**
(Lärbild-indigo), used when no subject is selected.

Per-subject hue values are **not** stored here — they live with the taxonomy in
`data.js` (`subjects[].hue`), the single source of truth for the taxonomy. Subject
cards (`.subj-card`) carry their own hue via a local `--h` variable the view sets
per card, so each card shows its own subject color regardless of the active view.

> Note: the handoff `styles.css` literal `--brand-h: 255` is a stale pre-tweak
> value the prototype overrides to 268 at runtime (`app.jsx`, default brand
> `#5145d6` → hue 268, which the reference screenshots reflect). 268 is the
> faithful rendered default and is what `tokens.css` uses.

## Token groups

- **Surface & ink** — `--bg`, `--bg-2`, `--surface`, `--surface-2`, `--sink`,
  `--border`, `--border-2`, `--ink`, `--ink-2`, `--muted`, `--faint`.
- **Subject color** — `--brand-h` + derived `--brand*`.
- **Status** — `--good`, `--warn`, `--danger` (+ soft/line/glow variants).
- **Overlays/misc** — `--overlay-light`, `--scrim`, `--scrim-shadow`,
  `--scrollbar*`, `--cool`.
- **Typography** — `--serif`/`--sans`/`--mono`; type scale `--text-display …
  --text-micro`; `--weight-*`, `--leading-*`, `--tracking-*`.
- **Spacing** — `--space-2 … --space-48` (suffix = px; mirrors the handoff
  ladder).
- **Form & elevation** — `--radius`, `--radius-sm`, `--shadow-sm/--shadow/--shadow-lg`.
- **Layout** — `--rail-w`, `--topbar-h`.

## Fidelity note

Component CSS keeps the handoff's exact per-element font sizes and paddings for
hi-fi fidelity (a few are off the named scale — e.g. `.upper` is 10.5px, `.stack`
gap is 14px). The named type/space scale tokens are the guide for composing
**new** views without scattering magic numbers; they are not a reason to "round"
existing component values.
