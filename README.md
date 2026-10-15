<img src="pcm/logo.svg" width="96" alt="">

# Fab Release

A KiCad 10 plugin for Windows that makes a manufacturing release of the board
open in the PCB editor.

It copies the saved board to a temporary folder and runs
[prefab-gate](https://github.com/danielboston38/prefab-gate) on the copy: DRC
with zone refill, schematic parity and a trace current check. Your project
files are not modified. If the gate passes, it exports the fab and assembly
files into a dated folder, zips the folder, and tags the git commit it was
made from.

The plugin uses KiCad's IPC API through
[kicad-python](https://pypi.org/project/kicad-python/), not the SWIG
scripting interface that KiCad 11 removes. Changes are listed in
[CHANGELOG.md](CHANGELOG.md).

## Release folder

```
Manufacturing/<board>_<rev>_<DDMMYY>/
  1_Fabrication/    Gerber and drill zip, PCB stackup, extra fab files
  2_Assembly/       position file, BOM CSV, iBOM, component data, assembly PDFs (top, bottom)
  3_Test/           test point CSV
  4_Documentation/  schematic PDF, STEP (zipped), renders (top, bottom, isometric)
  manifest.json     tool and KiCad versions, git commit, gate result, settings, SHA-256 of every file
Manufacturing/<board>_<rev>_<DDMMYY>.zip
```

The parent folder, the name pattern (`{board}`, `{rev}`, `{date}`) and the
set of outputs are set per board in the dialog.

## Checks

The release stops before anything is published if:

- the project has uncommitted changes (you can choose a trial release
  instead; its manifest is marked dirty and it gets no tag)
- the board has no rev in its title block
- prefab-gate reports a blocking finding (DRC exclusions set in KiCad are
  respected)
- the Gerber files are missing a copper layer, a mask, a silkscreen, the
  board outline or the plated drill file
- any export fails or does not produce its file

Files are written to a `.partial` folder and renamed only when every step has
succeeded, so a failed or cancelled run leaves no half-made release.

If a release with the same name exists, it is moved to the Recycle Bin. If the
git tag exists, the release is made without a tag and the dialog says so.

## Compared with a KiCad jobset

KiCad 9 and 10 can run a jobset (`.kicad_jobset`) from the project manager or
with `kicad-cli jobset run`. A jobset runs KiCad's exports and checks in order
and writes them to a folder or an archive. Fab Release uses the same
`kicad-cli` exports and adds the checks and records around them.

| | KiCad jobset | Fab Release |
|---|---|---|
| Platforms | Windows, Linux, macOS | Windows |
| Setup | Built into KiCad | Plugin, git, and iBOM for the iBOM output |
| Exports | All KiCad formats, including ODB++, IPC-2581 and DXF | Gerber, drill, position, BOM, PDFs, STEP, renders |
| DRC | DRC job with zone refill and schematic parity; can fail the run | prefab-gate: DRC with zone refill and schematic parity, plus a trace current check |
| ERC | ERC job | Not run |
| Gerber layer check | No; plots the layers the job lists | Stops if a copper layer, mask, silkscreen, outline or plated drill is missing |
| Git | No | Requires committed files, records the commit, tags it |
| Rev | Not checked | Required; used in the folder name and the tag |
| Test points, component data, stackup, iBOM | Only through an "execute command" job running your own script | Included |
| Record of the release | No | `manifest.json` with versions, commit, gate result and file hashes |
| Settings | The jobset file | `<board>.release.json` beside the board |

Use a jobset if you need only the exports, need ERC or another format, or work
on Linux or macOS. Use Fab Release if you want the release blocked by the
gate and the Gerber check, and tied to a git commit with a manifest.

## Requirements

- KiCad 10.0 on Windows
- The KiCad API turned on: Preferences > Plugins > Enable KiCad API. The page
  then shows "Listening at ipc://...".
- git on `PATH`, with the board in a git repository
- Interactive HTML BOM from the Plugin and Content Manager, for the iBOM output

## Installation

1. Download `fab-release-<version>-pcm.zip` from
   [Releases](https://github.com/sijah/Fab-release/releases).
2. In KiCad, open the Plugin and Content Manager, choose Install from File,
   and select the zip.
3. Restart KiCad if the toolbar button does not appear.

The first time the plugin loads, KiCad creates a Python environment for it and
installs `kicad-python`. The button appears when that has finished.

If the button still does not appear:

- Preferences > Plugins > Python interpreter should point at KiCad 10's
  `pythonw.exe`, for example `C:\Program Files\KiCad\10.0\bin\pythonw.exe`.
  Detect Automatically sets it.
- Preferences > PCB Editor > Plugins: right-click the action and choose
  Recreate Plugin Environment.

## Use

1. Commit the project and open the board in the PCB editor.
2. Click the Fab Release button on the toolbar.
3. Check the release name and outputs, then click Release.

Progress is shown in the dialog. Cancel stops the run and publishes nothing.
When the release is done, the dialog offers to open its folder.

### Settings

The dialog saves its settings beside the board as `<board>.release.json`.
Commit this file so everyone makes the release the same way. Changes to it do
not count as uncommitted changes.

The release folder is set at the top of the dialog and the outputs on the
Outputs tab. The BOM, name, prefab-gate tab sets:

- BOM: a BOM preset saved in the schematic, or the fields, column titles and
  grouping
- the name pattern of the release folder
- extra fab files, copied into `1_Fabrication` on every release and separated
  by `;`. A file name on its own refers to a file shipped with the plugin
  (`Required_impedance_control.xlsx`, a blank impedance control sheet); a full
  path is used as written.
- prefab-gate options: strict mode (cosmetic findings also block), schematic
  parity, trace current

The Theme list at the bottom of the dialog switches between System, Light and
Dark. Changing it reopens the dialog.

Test points are pads whose Fabrication property is set to Test point pad.

### Command line

The same release can run without the KiCad window:

```
python release.py path\to\board.kicad_pcb [--allow-dirty] [--no-tag]
```

Exit codes: 0 released, 2 blocked by the gate, 3 could not run, 4 refused,
5 an export failed.

### Claude Code

The repository is also a Claude Code plugin. Install it with:

```
/plugin marketplace add sijah/Fab-release
/plugin install fab-release@fab-release
```

Start a new session so the plugin loads. It needs KiCad 10, git and, for the
iBOM, the Interactive HTML BOM plugin, as above.

Ask in plain words, or call the skill as
`/fab-release:fab-release path\to\board.kicad_pcb`:

- "make a fab release of path\to\board.kicad_pcb", or "release this board"
  in the board's folder (Claude finds the `.kicad_pcb`, and asks if there
  are several)
- "trial release, it isn't committed yet" adds `--allow-dirty`
- "release without tagging" adds `--no-tag`

Claude runs the command line above from the plugin's own copy. If the gate
blocks, it lists the findings and stops; it does not export around them. If
the release is refused, it says what to fix, such as committing or setting
the rev.

Settings can be changed the same way, for example "turn off STEP and iBOM",
"use BOM preset Production", "put releases in D:\Releases" or "make the gate
strict". Claude edits `<board>.release.json`, the same file the dialog
writes.

The plugin also reviews schematics: ask "review the schematic of
path	ooard.kicad_pcb". `scripts/sch_facts.py` turns the kicad-cli netlist
and ERC into one summary (every part, pin and net, plus mechanical red flags
such as floating pins), and Claude checks each IC against its datasheet:
power pins, decoupling, straps and pull-ups, voltage levels between rails,
connectors and ESD. It writes `<board>.schematic-review.md` with findings
ranked likely bug, check and nit. The review is advice only: it does not edit
the schematic or block a release.

Update with `/plugin marketplace update fab-release`; turn off with
`/plugin disable fab-release@fab-release`; remove with
`/plugin uninstall fab-release@fab-release`.

## Building the package

```
git clone https://github.com/sijah/Fab-release
cd Fab-release
python pcm/build_pcm.py
python -m unittest test_release
```

prefab-gate is included in `prefab_gate/`: upstream's `scripts/` with
`pcm/prefab-gate-kicad10-excluded.patch` applied. The patch makes it treat
KiCad 10 DRC exclusions (`"excluded": true`) as exclusions; earlier versions
used the severity `exclusion`. Replace the folder with upstream's `scripts/`
once prefab-gate includes the change.

`build_pcm.py` writes `dist/fab-release-<version>-pcm.zip`. For a submission
to KiCad's add-on repository it also writes
`dist/kicad-submission/<identifier>/`, holding the package's `metadata.json`
(with the download URL, SHA-256 and sizes) and `icon.png`. Raise `version` in `pcm/metadata.json` for every package you
distribute.

## Credits

- [prefab-gate](https://github.com/danielboston38/prefab-gate) by Mark Dymek,
  MIT license, included in `prefab_gate/` and the package
- [Interactive HTML BOM](https://github.com/openscopeproject/InteractiveHtmlBom)
  by openscopeproject, used when installed

## License

MIT, © 2026 Sijah AK ([github.com/sijah](https://github.com/sijah)). See
[LICENSE](LICENSE).
