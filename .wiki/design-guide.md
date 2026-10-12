# LabOnoM Design Guide (pinned)

AXIO Stitching Studio follows the **LabOnoM Design Guide**, the one design system for everything Nexitia and LabOnoM
ship: private repository `LabOnoM/design` (clone `~/GitHub/design`), version **2026.10.0**, pinned by
`DESIGN_VERSION` at the repository root and verified by `scripts/design-pin-check.sh`.

- `DESIGN.md` at the root is the pinned family file "AROS Deep Space". Never edit it here; propose the
  change in the guide (its `PROCESS.md`).
- Profile: the "Lab Canvas" profile, through the generated Qt palette `dist/lab-canvas.qss` (vendored here as `design/lab-canvas.qss`).
- Words come from the guide's `GLOSSARY.md` (English and Japanese); states from `STATES.md`; the measured
  checks this repository keeps green are in `CONFORMANCE.md`.
- Process: the `ux-foundations` skill's P1 brief before any change a person sees, P2 and P3 audits in both
  themes after; findings become SPEC clauses with measured tests.

Updating to a newer release: copy `DESIGN.md` from the guide, set `DESIGN_VERSION` to the line
`<version> <sha256>` from its `RELEASES.json`, run `scripts/design-pin-check.sh`, port the new conformance
items.

History: until 2026-10-12 the family file was copied here from `~/GitHub/DESIGN.md` by a script, and the
"Nexitia Public Identity" section lived only in the federation's copy for three weeks. The guide's
repository and the hash pin replaced that (design workstream W14, step three).
