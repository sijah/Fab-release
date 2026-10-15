# Fab Release

One-click KiCad 10 release tool: prefab-gate check → kicad-cli exports → fab
data (stackup, component data, test points), iBOM →
`Manufacturing/<board>_<rev>_<DDMMYY>/` (1_Fabrication, 2_Assembly, 3_Test,
4_Documentation; folder configurable). User-facing docs: `README.md`.

Local-only notes (internal paths, reference boards) live in the gitignored
`.private/`; read `.private/CLAUDE-internal.md` when it exists. This repo is
public: never commit private paths, board or product names, emails or
customer data.

## Status

- Run: `python release.py <board>.kicad_pcb [--allow-dirty] [--no-tag]`
  (exit 0 released, 2 gate blocked, 3 could not run, 4 refused, 5 step failed).
- Button: KiCad 10 **IPC API** plugin (`pcm/plugin/plugin.json`, `ipc_entry.py`,
  kipy) — never SWIG `pcbnew.ActionPlugin`. Build: `python pcm/build_pcm.py`,
  then KiCad → Plugin and Content Manager → Install from File →
  `dist\fab-release-<ver>-pcm.zip`. Bump `version` in `pcm/metadata.json`
  for every package you hand out, and the same
  `version` in `.claude-plugin/plugin.json` + `marketplace.json`. The build also writes
  `dist/kicad-submission/` for KiCad's add-on index.
- Per-board settings: `<board>.release.json` beside the board, written by the
  button's dialog, read by the CLI too; never counts as uncommitted; recorded in
  the manifest.
- Tests: `python -m unittest test_release -v` (no KiCad needed).
- Needs PCM plugin iBOM; found under
  `<Documents>\KiCad\10.0\3rdparty\plugins` (Documents from the registry) or
  `%KICAD10_3RD_PARTY%\plugins`.
- `.e2e/` is the gitignored scratch area for end-to-end runs; `.e2e/golden/`
  holds a real board and its reference test points for the golden test.
- Icons are drawn in `pcm/build_pcm.py` (stdlib) per KiCad's icon guidelines;
  `pcm/logo.svg` has the same geometry.
- `fab/` holds files shipped with the plugin (blank impedance control sheet);
  a bare name in Extra fab files resolves there.

## What's here

- `prefab_gate/` — vendored upstream gate (github.com/danielboston38/prefab-gate
  `scripts/`, MIT) with `pcm/prefab-gate-kicad10-excluded.patch` applied: KiCad 10
  DRC exclusions (`"excluded": true`) must not block. Used by release.py, the PCM
  build and the Claude plugin. Swap back to upstream once that fix is merged.
  A local clone in gitignored `prefab-gate/` is optional (for upstreaming only).
  Run: `python prefab_gate/prefab_gate.py check|package <board>.kicad_pcb`.
- `.claude-plugin/` + `skills/fab-release/SKILL.md` — Claude Code plugin and
  marketplace (repo root); the skill runs `release.py` via `${CLAUDE_PLUGIN_ROOT}`.
  Versions must equal `pcm/metadata.json` (tested). `skills/schematic-review`
  runs `scripts/sch_facts.py <board> <out dir>` (kicad-cli netlist + ERC ->
  facts.json, schematic.pdf, page PNGs via optional PyMuPDF); advice only.
- `scripts/fab_exports.py` — stdlib, reads the `.kicad_pcb`/`.kicad_pro` text
  (no pcbnew): `python scripts/fab_exports.py components|testpoints|stackup
  <board.kicad_pcb> <out>`. Replaces older SWIG fab-data scripts and the
  TheJigsApp kicad-testpoints plugin (test points byte-identical to it).

## Rules

- KiCad code targets KiCad 10 and the IPC API, not SWIG. Headless work on the
  staged copy is kicad-cli or reading the board file as text (`fab_exports.py`);
  IPC needs the GUI. Only iBOM (third-party) is still SWIG.
- `prefab-gate check` refills zones and **saves the board** — always run it on
  a staged/temp copy, never the user's project files.
- Panel (KiKit) is out of scope.
- Windows only (Recycle Bin, registry, `python.exe`); declared in
  `pcm/metadata.json` `platforms`.
