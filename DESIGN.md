---
version: 2026.10.0
name: AROS Deep Space
description: >
  Unified visual identity for the AROS product family (AROS core dashboards,
  AROS-Cloud-Federation, AROS-IDE, AROS-Knowledge-Bank, extensions and docs).
  Dark-first glassmorphism with an indigo brand accent and a violet
  agent-activity accent. Derived from the audited tokens of
  AROS-Cloud-Federation/DESIGN_SPEC.md — the family's most mature system —
  and reconciled against every other shipped AROS surface.
  The family file of the LabOnoM Design Guide (github.com/LabOnoM/design); the
  Lab Canvas profile in profiles/lab-canvas/ derives from it for the desktop
  canvas tools.
colors:
  # ---- Neutrals (dark theme = default brand mode) ----
  background: "#09090B"          # hsl(240 10% 3.9%)
  card: "#0B0B0E"                # hsl(240 10% 5%)
  surface: "#18181B"             # hsl(240 6% 10%) — raised neutral between background and card
  border: "#27272A"              # hsl(240 3.7% 15.9%) — also input borders
  foreground: "#FAFAFA"          # primary text
  muted-foreground: "#A1A1AA"    # secondary text — 7.8:1 on background
  # ---- Neutrals (light theme) ----
  background-light: "#F7F7F8"
  card-light: "#FFFFFF"
  surface-light: "#EFEFF0"
  border-light: "#DFDFE2"
  foreground-light: "#09090B"
  muted-foreground-light: "#71717A"   # 4.5:1 on background-light — do not lighten
  # ---- Brand ----
  primary: "#9580FF"             # Vibrant Indigo, dark mode — hsl(250 100% 75%)
  primary-light: "#4F30E8"       # Indigo, light mode — hsl(250 80% 55%)
  primary-foreground: "#17171B"  # text on primary (dark mode)
  primary-foreground-light: "#FAFAFA"
  agent: "#8B5CF6"               # Violet — AI/agent activity accent, dark mode (4.7:1 on background)
  agent-light: "#7C3AED"         # Violet, light mode (5.3:1 on background-light)
  accent: "#CC66FF"              # Neon Purple, dark mode — marketing emphasis only
  accent-light: "#A219E6"
  # ---- Semantic status (reserved — never reused as product/sector accents) ----
  success: "#12D393"             # emerald, dark mode
  success-light: "#0C8D62"
  warning: "#F6A823"             # amber, dark mode
  warning-light: "#CE7C09"
  destructive: "#EF4444"
  destructive-light: "#DC2626"
  info: "#60A5FA"                # blue, dark mode (7.8:1 on background)
  info-light: "#2563EB"
  executing: "#06B6D4"           # cyan — actively-executing state (IDE/DAG surfaces), distinct from queued/running blue
  executing-light: "#0E7490"
  # ---- On-tint text (AA-safe small text on 10%-alpha tinted chips) ----
  on-tint-success: "#34D399"
  on-tint-success-light: "#065F46"
  on-tint-warning: "#FBBF24"
  on-tint-warning-light: "#92400E"
  on-tint-danger: "#F87171"
  on-tint-danger-light: "#B91C1C"
  on-tint-info: "#93C5FD"
  on-tint-info-light: "#1E40AF"
typography:
  display:
    fontFamily: Inter
    fontSize: 4rem
    fontWeight: 900
    lineHeight: 1.05
    letterSpacing: -0.02em
  h1:
    fontFamily: Inter
    fontSize: 2.25rem
    fontWeight: 800
    lineHeight: 1.15
    letterSpacing: -0.02em
  h2:
    fontFamily: Inter
    fontSize: 1.5rem
    fontWeight: 700
    lineHeight: 1.25
  h3:
    fontFamily: Inter
    fontSize: 1.125rem
    fontWeight: 600
    lineHeight: 1.35
  body:
    fontFamily: Inter
    fontSize: 1rem
    fontWeight: 400
    lineHeight: 1.6
  body-sm:
    fontFamily: Inter
    fontSize: 0.875rem
    fontWeight: 400
    lineHeight: 1.5
  dense:
    fontFamily: Inter
    fontSize: 0.75rem
    fontWeight: 400
    lineHeight: 1.4
  label-caps:
    fontFamily: Inter
    fontSize: 0.6875rem
    fontWeight: 700
    lineHeight: 1.2
    letterSpacing: 0.08em
  code:
    fontFamily: JetBrains Mono
    fontSize: 0.8125rem
    fontWeight: 400
    lineHeight: 1.55
  metric:
    fontFamily: JetBrains Mono
    fontSize: 2.5rem
    fontWeight: 600
    lineHeight: 1.1
rounded:
  xs: 4px
  sm: 8px
  md: 12px
  lg: 16px
  xl: 24px
  full: 999px
spacing:
  xs: 4px
  sm: 8px
  md: 16px
  lg: 24px
  xl: 40px
  2xl: 64px
components:
  button-primary:
    backgroundColor: "{colors.primary}"
    textColor: "{colors.primary-foreground}"
    typography: "{typography.body-sm}"
    rounded: "{rounded.md}"
    padding: 8px 16px
  button-primary-hover:
    backgroundColor: "#8A70FF"
    textColor: "{colors.primary-foreground}"
    rounded: "{rounded.md}"
  button-secondary:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.foreground}"
    typography: "{typography.body-sm}"
    rounded: "{rounded.md}"
    padding: 8px 16px
  button-ghost:
    backgroundColor: transparent
    textColor: "{colors.muted-foreground}"
    rounded: "{rounded.sm}"
    padding: 6px 10px
  card:
    backgroundColor: "{colors.card}"
    textColor: "{colors.foreground}"
    rounded: "{rounded.md}"
    padding: "{spacing.lg}"
  card-glass:
    backgroundColor: "rgba(15, 15, 25, 0.6)"
    textColor: "{colors.foreground}"
    rounded: "{rounded.lg}"
    padding: "{spacing.lg}"
  input:
    backgroundColor: "{colors.background}"
    textColor: "{colors.foreground}"
    typography: "{typography.body-sm}"
    rounded: "{rounded.sm}"
    padding: 8px 12px
  badge-status:
    typography: "{typography.label-caps}"
    rounded: "{rounded.sm}"
    padding: 2px 8px
  badge-success:
    backgroundColor: "rgba(18, 211, 147, 0.12)"
    textColor: "{colors.on-tint-success}"
    typography: "{typography.label-caps}"
    rounded: "{rounded.sm}"
    padding: 2px 8px
  badge-warning:
    backgroundColor: "rgba(246, 168, 35, 0.12)"
    textColor: "{colors.on-tint-warning}"
    typography: "{typography.label-caps}"
    rounded: "{rounded.sm}"
    padding: 2px 8px
  badge-danger:
    backgroundColor: "rgba(239, 68, 68, 0.12)"
    textColor: "{colors.on-tint-danger}"
    typography: "{typography.label-caps}"
    rounded: "{rounded.sm}"
    padding: 2px 8px
  badge-info:
    backgroundColor: "rgba(96, 165, 250, 0.12)"
    textColor: "{colors.on-tint-info}"
    typography: "{typography.label-caps}"
    rounded: "{rounded.sm}"
    padding: 2px 8px
  badge-agent:
    backgroundColor: "rgba(139, 92, 246, 0.15)"
    textColor: "{colors.agent}"
    typography: "{typography.label-caps}"
    rounded: "{rounded.sm}"
    padding: 2px 8px
  micro-label:
    textColor: "{colors.muted-foreground}"
    typography: "{typography.label-caps}"
  code-block:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.foreground}"
    typography: "{typography.code}"
    rounded: "{rounded.sm}"
    padding: "{spacing.md}"
---

## Overview

AROS products share one identity: **"Deep Space Biological"** — a dark-first,
near-black canvas with an indigo brand accent, a violet accent reserved for
AI/agent activity, frosted-glass elevation, and dense engineering typography.
This file is the single source of truth for every AROS surface: the Federation
marketplace, the AROS ops dashboards, the AROS-IDE (Theia and SPA frontends),
the Knowledge-Bank sites, benchmark dashboards, VS Code webviews, and marketing
pages.

Intentionally out of scope here: iconography (lucide-react on web,
codicons/Theia icons in the IDE — keep per-platform), illustration, and
motion timing beyond the durations named in Elevation & Depth. The `-light`
suffixed color tokens are consumed by each platform's light-theme binding
(they intentionally have no component references in this file).

Three rules govern everything else:

1. **Dark is the brand default; light is a first-class citizen.** Product
   surfaces ship both themes, switched by a `.dark` class the app controls —
   never by OS preference alone. Marketing/editorial pages may commit to one
   mode, but must use these tokens.
2. **Semantic colors are reserved.** Emerald means success/owned, amber means
   warning/metered, red means error/destructive, blue means info/running.
   No product, sector, or feature may adopt a reserved hue as its identity
   accent.
3. **One system, one language.** Products differentiate by accent and content,
   never by inventing new neutrals, fonts, radii, or status colors.

The complete interaction-level specification (commerce states, access badges,
component APIs, ARIA patterns) lives in
`AROS-Cloud-Federation/DESIGN_SPEC.md`; this file is the family-wide token
layer that spec now inherits from.

### Nexitia Public Identity

The public website uses the Nexitia company identity above the AROS product system: a white geometric N on indigo and a theme-aware `nexitia.ai` wordmark. Navbar and footer share `BrandLogo`; the mark remains 32-36px and its enclosing link is named "Nexitia home". This does not rename the AROS products or their operational interfaces.

The homepage campaign header uses paired local glass-N raster assets: silver on charcoal with white HTML type in dark mode, and deep indigo on pearl-white with charcoal type in light mode. The small N has matching white-on-indigo and indigo-on-pearl colorways, not separate identities. Scoped header color variables adapt buttons, supporting text, focus outlines and the foundation strip. The existing root `.dark` class selects the artwork with no layout-size change; operating-system preference does not override the saved site choice. The thesis is "Train the procedure, not the weights." No glow blobs, version-live badges, benchmark promises or inventory counts belong in this header. Text has zero letter spacing and fixed breakpoint sizes. See [Nexitia brand guide](brand/nexitia-brand.md).

## Colors

**Neutrals** are desaturated blue-gray (hue 240) — never pure gray, never pure
black. `background` is the page canvas, `card` the primary container,
`surface` the raised neutral between them (hover states, code wells, neutral
chips), `border` for all hairlines and inputs.

**Indigo (`primary`)** is the brand: primary CTAs, links, focus rings, active
navigation, and the "included in subscription" state. **Violet (`agent`)** is
the AI-presence color: agent chat accents, thinking/streaming states, swarm
and DAG agent nodes, "AI is working" glows. Keeping these adjacent hues in
separate jobs is deliberate — a user should be able to tell "the product wants
my attention" (indigo) from "an agent is acting" (violet) at a glance.
**Neon purple (`accent`)** is for marketing emphasis (gradients, hero glows)
only — never for functional UI states.

**Status colors** map one-to-one to meaning: `success` (also: owned,
completed, live), `warning` (also: metered, waiting, degraded), `destructive`
(failed, destructive actions), `info` (running, informational). Execution
surfaces (IDE, DAG canvases, swarm visualizers) additionally distinguish
`executing` (cyan — a step actively doing work right now) from `info`/running
(blue — queued, streaming, or in-progress at the job level). The status
badge pattern is a 10–15%-alpha tint of the status color as background, a
1px 20–30%-alpha border, and the matching `on-tint-*` token for text — the
on-tint tokens exist because raw status hues fail WCAG AA as small text on
tinted light-mode chips.

Contrast floors, both themes: body text ≥ 4.5:1 against its background,
large text and UI glyphs ≥ 3:1. The dark `muted-foreground` (7.8:1) and light
`muted-foreground-light` (4.5:1) are at their floors — do not lighten them.

## Typography

**Inter** is the family UI face; **JetBrains Mono** is the family mono —
code, log wells, run IDs, metrics, and tabular numbers. Load both explicitly
(next/font, Google Fonts, or bundled) — never let mono fall back to the
platform default, and never ship Arial/Roboto/Outfit on new surfaces.

Two densities, one scale:

- **Product density** (dashboards, IDE, consoles): `dense` (12px) is the
  working body size, `body-sm` for prose, `label-caps` for the signature
  uppercase letter-spaced micro-headers, `metric` for hero numbers.
  Never go below 11px for anything a user must read; 9–10px is reserved for
  decorative meta a user can ignore.
- **Marketing density** (landing pages, docs, papers): `body` at 1.6
  line-height, `display`/`h1` with tight tracking, gradient-clipped headline
  text (`primary → agent → info` direction 135deg) as the signature flourish.

## Layout

Spacing steps on the `spacing` scale — 4/8/16/24/40/64. Product surfaces are
compact: 8px within groups, 16px between groups, 24px panel padding.
Marketing sections breathe: 64px+ vertical rhythm, content max-width
72rem (max-w-7xl) with 16/24/32px responsive gutters.

Panels and sidebars are bordered, not shadowed: the 1px `border` hairline is
the primary structural device on product surfaces. Sticky chrome uses one
z-order ladder: page banners and top nav at 50, in-page toolbars at 30,
mobile action bars at 40, dialogs in the native top layer, toasts above all.

Every interactive element gets a unique, descriptive `id` (all AROS repos are
agent-driven and E2E-tested; this is load-bearing, not cosmetic). Responsive
floors: test at 320 / 768 / 1024.

## Elevation & Depth

Depth comes from **glass, glow, and border — not gray drop shadows.**

- **Glass** (the signature): translucent panel `rgba(15,15,25,0.6)` +
  `backdrop-filter: blur(16px)` + 1px `rgba(255,255,255,0.08)` border on dark;
  `rgba(255,255,255,0.6)` + `rgba(0,0,0,0.06)` border on light. Reserve glass
  for hero cards, overlays, and feature panels — flat `card` is the default
  container.
- **Glow** signals state, in the state's own color: focus ring
  `0 0 0 2px` in `primary`; agent-working pulse in `agent` at 15–30% alpha;
  status glows only on live/streaming elements.
- **Hover lift** on interactive cards: `translateY(-4px)` + a soft tinted
  shadow (brand-tinted, ~10% alpha), 150–200ms ease-out.

All motion (pulse, shimmer, lift, gradient rotation) must be wrapped in a
`prefers-reduced-motion: reduce` guard that collapses it to none.

## Shapes

The `rounded` scale maps to element size, consistently across products:
`xs` 4px for tiny controls and badges in dense IDE surfaces, `sm` 8px for
inputs, buttons-in-tables, code wells, `md` 12px for buttons and standard
cards, `lg` 16px for feature/glass cards, `xl` 24px for hero panels and
top-level dashboard cards, `full` for pills, avatars, and status dots.
Pick from the scale — 3px, 5px, 10px, 14px, 20px one-offs are drift.

## Components

The YAML `components` block defines the canonical recipes. Notes:

- **Buttons**: primary = indigo fill; secondary = `surface` fill + border;
  ghost for toolbars. Hover darkens/lightens within the same hue — never
  changes hue.
- **Status badges** (`badge-status`): tint + border + on-tint text as defined
  in Colors; always icon **and** text — color is never the only signal.
- **Cards**: flat `card` by default; `card-glass` for featured content.
  Card footers separate with a 1px border, not a background change.
- **Code blocks**: `surface` background in both themes — never
  `black/50 + text-blue-300` (fails light-mode AA).
- **Focus**: one global `:focus-visible` ring (2px `primary`, 2px offset) —
  no per-component focus styles.

Platform bindings — how these tokens reach each stack:

| Stack | Binding |
|---|---|
| Next.js / Tailwind v4 (Federation, hindsight, future AROS web) | `@theme` block in `globals.css` mapping `--color-* → hsl(var(--*))`, `.dark` class variant |
| Theia (AROS-IDE packages) | `--aros-*` custom properties layered over `--theia-*` bases; widgets consume `var(--aros-accent-agent, var(--theia-focusBorder))` etc. |
| Static HTML dashboards (runtime, antigravity, Benchmark) | one shared `aros-tokens.css` `:root` block — no per-file copies |
| VS Code webviews (swarm-dag) | map tokens onto `--vscode-*` variables so the panel follows the editor theme |
| MkDocs (Knowledge-Bank) | `palette.primary: deep purple → custom` via `extra_css` overriding `--md-primary-fg-color` to `primary` |

## Do's and Don'ts

**Do**

- Do consume tokens through each platform's semantic layer
  (`bg-primary`, `var(--aros-*)`) — never paste hex values into components.
- Do ship both themes on product surfaces, toggled by `.dark` class, with
  `@custom-variant dark` bound to the class (Tailwind v4).
- Do use `label-caps` uppercase micro-headers and JetBrains Mono metrics —
  they are the family's voice.
- Do state honestly when data is illustrative vs measured (Federation
  principle P5 — family-wide).
- Do run `design-md lint` (WCAG + token validation) on this file in CI and
  `design-md diff` on changes.

**Don't**

- Don't use emerald, amber, red, or blue as a product/sector/feature accent —
  they are reserved status hues.
- Don't introduce new fonts (Outfit, Roboto, Arial are legacy — migrate on
  touch), new neutrals, or off-scale radii.
- Don't hardcode a light or dark palette into a surface that lives inside a
  themed host (VS Code webviews, Theia widgets) — inherit the host tokens.
- Don't use color as the only signal for a state; pair it with an icon or
  label.
- Don't gate content with CSS blur — visual lock states are UX, the real gate
  is server-side (family security rule inherited from DESIGN_SPEC.md).
- Don't animate without a reduced-motion guard.

## Product Application Map

Current state and migration priority per surface (audited 2026-08-18):

| Surface | Today | Action |
|---|---|---|
| AROS-Cloud-Federation frontend | Source system — already compliant | Adopt this file as token source; add JetBrains Mono (its own spec §C1, still unshipped) |
| AROS-IDE Theia packages | Theia tokens + violet accent, but 4 competing hardcoded status palettes (Tailwind/Material/Dracula/Nord) | Define `--aros-status-*` once in aros-core; migrate widgets on touch |
| AROS-IDE Vite SPA | Coherent dark system, violet accent, Outfit font | Keep structure; swap Outfit→Inter, align status hues |
| AROS antigravity-dashboard + runtime static | Matching indigo #5E6AD2 glass identity, Outfit, copy-pasted CSS | Extract shared `aros-tokens.css`; accent #5E6AD2→primary; fix undefined `--text-*` vars |
| AROS Benchmark dashboards | Slate/blue/violet, 352-line style block duplicated ×3 | Same shared tokens file |
| aros-swarm-dag webview | Hardcoded light theme inside VS Code | Rebind to `--vscode-*` (real bug in dark editors) |
| AROS frontend (Next.js) | create-next-app scaffold ("E2E Test App") | Build the real dashboard on these tokens from day one |
| Knowledge-Bank SkillOpt pages | Polished light editorial (indigo #4F46E5, Fraunces) | Allowed as light-mode editorial; align accent to `primary-light` on next edit |
| Knowledge-Bank MkDocs site | Material deep-purple/cyan defaults | Repoint palette to `primary`/`agent` |
| AROS-test-gui | Automation fixture | Exempt — leave alone |
