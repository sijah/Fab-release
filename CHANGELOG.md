# Changelog — Fab Release

## v1.2.1 (2026-10-25)

- Fix: release no longer fails with "gerbers failed ... missing -F_Cu.gbr" when File > Plot
  uses Protel filename extensions (`.gtl`, `.gbl`...). The dialog now warns about it
  and proceeds on OK. It also warns when the board has no stackup set.
- Clear reasons when a release cannot run or finish: program not found (git, kicad-cli),
  a hung step (30 min timeout), wrong KiCad version, git "dubious ownership", locked or
  read-only output folder (the earlier release is no longer binned before that is
  checked), gate reasons and a capped item list, silent kills, errors inside the dialog,
  missing `.kicad_pro`, board name with a space, revs git cannot tag.
- The release adds every copper layer, mask, paste, silk and the outline to the plot in its staged copy, so
  nobody has to tick them in File > Plot and click Plot first; the dialog only notes it.
- A failed Claude review no longer cancels the release (advice only, as the dialog says).
- Fix: no false "plated drill missing" on SMD-only boards; the position file uses the
  Gerber origin only when File > Plot does; UTF-8 output so non-ASCII paths do not crash.
- Dialog warns when the board has no test-point pads while Test points is ticked.

## v1.2.0 (2026-10-16)

- Assembly PDFs: a mirrored bottom (`<board>_assembly_bottom_mirrored.pdf`, as
  seen holding the board bottom up) next to the see-through bottom.
- Renders: top, bottom and isometric are separate ticks (`render_top`,
  `render_bottom`, `render_iso`); an old `"renders"` setting still works.
- "Schematic PDF, fit to A4" (`schematic_pdf_a4`, off by default):
  `<board>_A4.pdf`, every sheet scaled onto A4 for printing, as File > Plot's
  "Page size: A4" (KiCad's own plot through a jobset; clickable links kept).

## v1.1.0 (2026-10-15)

- Optional Claude schematic review in the release: tick "Claude schematic review"
  (or `"claude_review": true` in `<board>.release.json`) and the release runs
  Claude Code headless on the staged schematic, putting
  `<board>_schematic_review.md` in `4_Documentation`. Off by default; advice only,
  never blocks. Needs Claude Code on PATH.

- Claude Code plugin: `skills/fab-release` runs a release from Claude Code
  (`/plugin marketplace add sijah/Fab-release`).
- Schematic review skill (`skills/schematic-review`) with
  `scripts/sch_facts.py`: netlist + ERC summary for a datasheet-based review.
- prefab-gate is vendored in `prefab_gate/` with the KiCad 10 exclusions fix;
  building no longer needs a separate clone.

## v1.0.0 (2026-10-15)

First public release, for KiCad 10.0 on Windows (IPC API).

- One toolbar button: choose outputs, run prefab-gate on a temporary copy of
  the saved board, then build `Manufacturing/<board>_<rev>_<DDMMYY>/` with
  `1_Fabrication`, `2_Assembly`, `3_Test` and `4_Documentation`, a zip of it
  all and a manifest, and tag the git commit.
- Outputs: Gerber + drill zip with a Gerber layer check, PCB stackup,
  position file, component data, test points, BOM CSV, iBOM (with MPN and DNP
  columns), schematic PDF, top/bottom assembly PDFs, STEP, renders.
- Per-board settings in `<board>.release.json`, shared through git.
- Extra fab files copied into `1_Fabrication`; a blank impedance control
  sheet ships with the plugin.
- An earlier release of the same name goes to the Recycle Bin; an existing
  git tag gives an untagged release with a warning.
- System / Light / Dark theme.
- Command line: `python release.py <board>.kicad_pcb`.
