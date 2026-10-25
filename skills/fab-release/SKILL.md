---
name: fab-release
description: Use to make a manufacturing release of a KiCad 10 board - verifies it with prefab-gate on a temporary copy, then exports Gerbers, drill, stackup, position file, BOM, iBOM, test points, schematic and assembly PDFs, STEP and renders into a dated Manufacturing folder, zips it and tags the git commit. Triggers on "release this board", "make a fab release", "manufacturing package", "release files for <board>", "prepare the board for fab/assembly", or a change to <board>.release.json settings.
---

# Fab Release

Never hand-export fab files. Run:

    python "${CLAUDE_PLUGIN_ROOT}/release.py" <board.kicad_pcb>

`${CLAUDE_PLUGIN_ROOT}` is required: the tool and its bundled prefab-gate ship
with the plugin, not with the user's project. Windows only. Needs KiCad 10
(kicad-cli is found by prefab-gate), git on PATH, and, when the iBOM output is
on, the Interactive HTML BOM plugin from KiCad's Plugin and Content Manager.

If the user gives no board, look for `*.kicad_pcb` under the current directory;
ask when there is more than one.

## Flags

- `--allow-dirty`: trial release of uncommitted work. Manifest is marked
  dirty, no tag. Only when the user asks for it - never to get past exit 4.
- `--no-tag`: release without creating the git tag.

## Exit codes

- `0` released. Report the folder and zip path from the output.
- `2` blocked by prefab-gate. Show the blocking findings. Nothing was
  published. Do not export by hand or turn findings off to get around it;
  the board has to be fixed in KiCad, committed, and the release rerun. If a
  finding the user excluded in KiCad still blocks, check `drc_exclusions` in
  the `.kicad_pro` on disk - the exclusion may not have been saved.
- `3` could not run: KiCad, git, iBOM or the board path is missing. Say which.
- `4` refused: uncommitted changes, no rev in the title block, or an invalid
  `<board>.release.json`. Say what to fix (commit, set the rev).
- `5` an export step failed or produced no file. Show its output.

The tool works on a temporary copy; the user's project files are never
modified. A run that fails publishes nothing.

## Output

    Manufacturing/<board>_<rev>_<DDMMYY>/
      1_Fabrication/    Gerber and drill zip, PCB stackup, extra fab files
      2_Assembly/       position file, BOM CSV, iBOM, component data, assembly PDFs
      3_Test/           test point CSV
      4_Documentation/  schematic PDF, STEP (zipped), renders
      manifest.json     tool/KiCad versions, commit, gate result, settings, SHA-256s
    Manufacturing/<board>_<rev>_<DDMMYY>.zip

An existing release of the same name goes to the Recycle Bin. If the tag
exists, the release is made untagged and the output says so.

## Settings: `<board>.release.json`

Beside the board; the KiCad button's dialog writes it, the CLI reads it.
Changes to it never count as uncommitted. Only include keys that differ from
the defaults; unknown keys or wrong types refuse with exit 4.

```json
{
  "outputs": {"gerber": true, "position": true, "component_data": true,
              "testpoints": true, "bom": true, "ibom": true,
              "schematic_pdf": true, "schematic_pdf_a4": false,
              "assembly_pdf": true, "step": true,
              "render_top": true, "render_bottom": true, "render_iso": true},
  "bom_fields": "Reference,Value,Footprint,${QUANTITY},${DNP},MPN",
  "bom_labels": "Reference,Value,Footprint,Qty,DNP,MPN",
  "bom_group_by": "Value,Footprint,MPN,${DNP}",
  "bom_preset": "",
  "folder": "Manufacturing",
  "name": "{board}_{rev}_{date}",
  "fab_extra": "Required_impedance_control.xlsx",
  "gate": {"strict": false, "parity": true, "ampacity": true},
  "claude_review": false
}
```

- `bom_preset`: a BOM preset saved in the schematic; replaces the three
  `bom_*` field settings when set.
- `folder`: relative to the board (`.` = beside it) or absolute.
- `fab_extra`: files copied into `1_Fabrication`, `;` between them. A bare
  name is a file shipped with the plugin; a full path is used as written.
- `gate.strict`: cosmetic findings block too. `parity`: schematic parity.
  `ampacity`: trace current check.
- `claude_review`: the release also runs the schematic-review skill through
  `claude -p` on the staged copy and puts
  `4_Documentation/<board>_schematic_review.md` in the release. Advice only;
  a failed review fails the step (exit 5), a missing `claude` exits 3.

Test points are pads whose Fabrication property is "Test point pad".
