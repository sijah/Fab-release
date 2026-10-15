<img src="pcm/logo.svg" width="96" alt="">

# Fab Release

A KiCad 10 plugin that turns the board you have open into a complete, checked
manufacturing release with one button.

It runs [prefab-gate](https://github.com/danielboston38/prefab-gate) (DRC, zone
refill, schematic parity, trace current) on a temporary copy of the saved
board, so your files are never written. If nothing blocks, it exports
everything a fab and an assembly house need into one dated folder, zips it,
and tags the git commit the release was made from.

Runs on KiCad's **IPC API** (via [kicad-python](https://pypi.org/project/kicad-python/)),
not the SWIG scripting runtime that KiCad removes in 11.0. See
[CHANGELOG.md](CHANGELOG.md).

## What a release contains

```
Manufacturing/<board>_<rev>_<DDMMYY>/
  1_Fabrication/    Gerber + drill zip, PCB stackup, extra fab files (impedance sheet)
  2_Assembly/       position file, BOM CSV, iBOM, component data, assembly PDFs (top, bottom)
  3_Test/           test points
  4_Documentation/  schematic PDF, STEP, renders (top, bottom, isometric), zip of the release
  manifest.json     tool and KiCad versions, git commit, gate result, settings, file hashes
```

The folder, the name pattern (`{board}`, `{rev}`, `{date}`) and every output
can be changed per board in the dialog.

Before exporting, it checks that:

- every file is committed to git (or you choose a trial release: marked dirty, no tag)
- the board has a rev in its title block
- prefab-gate finds nothing blocking; DRC exclusions you set in KiCad are honoured
- the Gerber plot includes every copper layer, both masks and silks, the outline and the plated drill

If the release folder already exists, the old one goes to the Recycle Bin.
If the git tag already exists, the release is made untagged and says so.

## Requirements

- **KiCad 10.0 on Windows**
- **Enable KiCad API**: Preferences → Plugins → check it once. The page then
  shows *Listening at ipc://…*
- **git** on `PATH`, and the board inside a git repository
- **Interactive HTML BOM** from the Plugin and Content Manager, if you want the iBOM output

## Installation

### Via PCM

1. Download `fab-release-<version>-pcm.zip` from
   [Releases](https://github.com/sijah/kicad-release/releases)
2. KiCad → **Plugin and Content Manager** → **Install from File…** → select the zip
3. Restart KiCad if the toolbar button doesn't appear

KiCad creates a Python environment for the plugin and installs
`kicad-python` the first time it loads; the button appears once that is done.

### If the toolbar button doesn't appear

- Preferences → Plugins → **Python interpreter** should point at KiCad 10's
  `pythonw.exe` (e.g. `C:\Program Files\KiCad\10.0\bin\pythonw.exe`). Use
  **Detect Automatically**.
- Preferences → PCB Editor → Plugins → right-click the action →
  **Recreate Plugin Environment**.

## Usage

1. Commit your project, then open the board in the PCB Editor
2. Click the **Fab Release** toolbar button
3. Check the release name and outputs, then **Release**

Progress is shown in the dialog; **Cancel** stops it and publishes nothing.
When it finishes you can open the release folder.

### Settings

The dialog saves its settings beside the board as `<board>.release.json`.
Commit it so everyone releases the board the same way; it never counts as an
uncommitted change. The **Outputs** tab picks what is made and the release
folder; the **BOM, name, prefab-gate** tab sets:

- **BOM**: a BOM preset saved in the schematic, or the fields, column titles and grouping
- **Name pattern** of the release folder
- **Extra fab files** copied into `1_Fabrication` every release, separated by `;`.
  A bare name is a file shipped with the plugin (`Required_impedance_control.xlsx`);
  a full path is used as written.
- **prefab-gate**: strict (cosmetic findings block too), schematic parity, trace current

**Theme** (System / Light / Dark) sits at the bottom of the dialog; changing it
reopens the dialog.

**Test points** are pads whose Fabrication property is *Test point pad*.

### Command line

The same release, without KiCad's GUI open:

```
python release.py path\to\board.kicad_pcb [--allow-dirty] [--no-tag]
```

Exit codes: 0 released, 2 gate blocked, 3 could not run, 4 refused, 5 an export failed.

## Building the package

```
git clone https://github.com/sijah/kicad-release
cd kicad-release
git clone https://github.com/danielboston38/prefab-gate
python pcm/build_pcm.py
python -m unittest test_release
```

`build_pcm.py` writes `dist/fab-release-<version>-pcm.zip` and, for a
submission to KiCad's add-on repository, `dist/kicad-submission/` with the
package's `metadata.json` (download URL, SHA-256 and sizes filled in) and
`icon.png`. Bump `version` in `pcm/metadata.json` for every package you hand out.

## Credits

- [prefab-gate](https://github.com/danielboston38/prefab-gate) by Mark Dymek (MIT), bundled
- [Interactive HTML BOM](https://github.com/openscopeproject/InteractiveHtmlBom) by openscopeproject, used when installed

## Author

Sijah AK — [github.com/sijah](https://github.com/sijah)

## License

MIT © 2026 Sijah AK. See [LICENSE](LICENSE).
