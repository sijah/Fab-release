# Changelog — Fab Release

## Unreleased

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
