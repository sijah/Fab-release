"""Atomic fab-package export.

Built in a staging directory inside the output directory and published with
os.replace only on success, so a failure never leaves something that looks
shippable. Staging lives inside out_dir (not the system temp dir) so the
final rename is same-filesystem and genuinely atomic.
"""
import glob
import os
import shutil
import subprocess
import tempfile
from datetime import datetime

from gate.kicad import KicadUnavailable

# Distinguishes "caller said there is no schematic" from "caller said nothing".
_DERIVE = object()


def schematic_for(board: str) -> str:
    """The schematic KiCad itself pairs with this board: same stem, .kicad_sch.

    This is not just a convention the gate follows — `kicad-cli pcb drc` derives
    the schematic from the board's basename and offers no way to point it
    elsewhere. A schematic under any other name cannot be used for parity at
    all, which is why locate_schematic's fallbacks still lead to a finding
    rather than a promise.
    """
    return os.path.splitext(board)[0] + ".kicad_sch"


def schematic_candidates(board: str, globber=glob.glob) -> list:
    """Every .kicad_sch sitting beside the board."""
    return sorted(globber(os.path.join(os.path.dirname(board) or ".", "*.kicad_sch")))


def locate_schematic(board: str, override=None, exists=os.path.exists,
                     globber=glob.glob):
    """The schematic to check parity against, or None if there isn't one.

    Roughly one KiCad project in five has no schematic under the board's own
    basename, so refusing to start on that basis would make the gate unusable
    on real corpora. It falls back to a sole .kicad_sch beside the board, and
    returns None rather than guessing when the directory holds several — the
    caller turns that into a finding, never a silent skip.
    """
    if override:
        if not exists(override):
            raise KicadUnavailable(
                f"--schematic points at {override!r}, which does not exist")
        return override
    conventional = schematic_for(board)
    if exists(conventional):
        return conventional
    candidates = schematic_candidates(board, globber)
    return candidates[0] if len(candidates) == 1 else None


def _run(runner, cmd):
    result = runner(cmd, capture_output=True, text=True)
    if getattr(result, "returncode", 0) != 0:
        raise KicadUnavailable(
            f"{' '.join(cmd[1:4])} failed (exit {result.returncode}): "
            f"{(getattr(result, 'stderr', '') or '').strip()}")


def _describe(path: str) -> str:
    if not os.path.isdir(path):
        return "a file"
    try:
        return "a directory" if os.listdir(path) else "an empty directory"
    except OSError:
        return "a directory"


def export_package(cli: str, board: str, out_dir: str, runner=subprocess.run,
                   on_staged=None, schematic=_DERIVE) -> str:
    """Export a fab package into <out_dir>/<timestamp>/, atomically.

    on_staged, if given, is called with the staging directory once every
    export has succeeded and before the directory is published. Anything
    that must be inside the package — the manifest — is written there, so
    the single os.replace publishes the package and its receipt together.
    A failure in the callback leaves nothing behind, exactly like a failed
    export: there is no window in which a complete-looking package exists
    without its receipt.

    schematic names the file the BOM comes from. Unlike `pcb drc`, `sch export
    bom` does take a path, so the caller passes whatever locate_schematic
    found rather than assuming the board's basename. None means there is no
    schematic and the BOM is skipped — a PCB-only board is a legitimate thing
    to package, and failing on a missing sibling after three successful
    exports reported it as a broken kicad-cli install.
    """
    if schematic is _DERIVE:
        schematic = schematic_for(board)
    stamp = datetime.now().strftime("%Y-%m-%d-%H-%M-%S")
    final = os.path.join(out_dir, stamp)
    os.makedirs(out_dir, exist_ok=True)
    staging = tempfile.mkdtemp(prefix=".staging-", dir=out_dir)
    try:
        _run(runner, [cli, "pcb", "export", "gerbers", "--board-plot-params",
                      "-o", os.path.join(staging, "gerbers"), board])
        _run(runner, [cli, "pcb", "export", "drill", "--format", "excellon",
                      "--excellon-units", "mm", "--generate-map",
                      "-o", os.path.join(staging, "drill"), board])
        _run(runner, [cli, "pcb", "export", "pos", "--format", "csv", "--units", "mm",
                      "--side", "both", "-o", os.path.join(staging, "cpl.csv"), board])
        if schematic is not None:
            _run(runner, [cli, "sch", "export", "bom", "--group-by", "",
                          "--fields", "Reference,Value,Footprint,Manufacturer,MPN,LCSC,Datasheet",
                          "-o", os.path.join(staging, "bom.csv"), schematic])
        if on_staged is not None:
            on_staged(staging)
        # os.replace happily renames onto an existing *empty* directory, which
        # would silently claim a path someone else is using. Check first, and
        # keep the OSError catch as a backstop for the non-empty case and races.
        if os.path.lexists(final):
            raise KicadUnavailable(
                f"cannot publish the package to {final}: {_describe(final)} already "
                "exists there. A package from the same second is still on disk — "
                "wait a moment and run again, or move it aside.")
        try:
            os.replace(staging, final)
        except OSError as exc:
            raise KicadUnavailable(
                f"cannot publish the package to {final}: {exc.strerror or exc}. "
                "A package from the same second already exists there — wait a moment "
                "and run again.") from exc
        return final
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
