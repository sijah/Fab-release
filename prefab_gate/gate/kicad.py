"""Locating and driving kicad-cli."""
import glob
import re
import json
import os
import shutil
import subprocess
import tempfile

# Every flag run_drc passes unconditionally. Probing a subset would let the gate
# start on a kicad-cli that then fails on the first real invocation.
REQUIRED_FLAGS = ("--format", "--schematic-parity", "--refill-zones",
                  "--save-board", "--severity-all")

# kicad-cli exits 0 and writes "schematic_parity": [] both when the parity
# tests ran and found nothing and when they could not run at all, so neither
# its exit code nor the report can tell those apart. This line on *stdout*
# can: verified against KiCad 10.0.6, it is printed whenever the tests ran —
# including "Found 0 schematic parity issues" for a clean board — and is
# absent entirely when they did not run.
#
# Matching it makes the invariant positive: parity must be affirmed to have
# run. The gate used to do the opposite, recognising failure from stderr
# wording, which meant any rewording of a diagnostic that is not an API
# turned a board whose schematic never loaded into a clean pass. Keying on
# the affirmation reverses which way an unfamiliar kicad-cli falls: if this
# line changes, the gate reports parity unproven instead of waving it past.
PARITY_RAN = re.compile(r"^Found \d+ schematic parity issues", re.M)

# Checked in order, after $KICAD_CLI and $PATH.
FALLBACK_PATHS = (
    "/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli",
    "/usr/bin/kicad-cli",
    "/usr/local/bin/kicad-cli",
)

# KiCad on Windows installs under a version-numbered directory, so no single
# path is right across releases. Globbed and newest-first rather than pinned.
WINDOWS_GLOBS = (
    r"C:\Program Files\KiCad\*\bin\kicad-cli.exe",
    r"C:\Program Files (x86)\KiCad\*\bin\kicad-cli.exe",
)


def _windows_version_key(path: str):
    """Sort globbed Windows installs newest-first, numerically not lexically.

    Plain string sorting puts KiCad 9.0 above 10.0, which is exactly backwards.
    """
    match = re.search(r"[\\/]KiCad[\\/]([0-9][0-9.]*)[\\/]", path)
    return tuple(int(n) for n in match.group(1).split(".") if n.isdigit()) if match else ()


class KicadUnavailable(Exception):
    """kicad-cli is missing, too old, or unable to do what the gate needs."""


class BoardUnreadable(Exception):
    """kicad-cli ran and refused the board — bad input, not a bad environment.

    Deliberately not a KicadUnavailable: by the time run_drc is called,
    locate_cli and probe_capability have already established that kicad-cli
    exists and supports every flag the gate passes. A non-zero exit from the
    DRC itself is therefore about this board, and telling the user to go fix
    their KiCad install would send them somewhere there is nothing to fix.
    """


def locate_cli(env=None, which=shutil.which, exists=os.path.exists,
               globber=glob.glob) -> str:
    env = os.environ if env is None else env
    override = env.get("KICAD_CLI")
    if override:
        if exists(override):
            return override
        raise KicadUnavailable(f"KICAD_CLI is set to {override!r}, which does not exist")
    found = which("kicad-cli")
    if found:
        return found
    for candidate in FALLBACK_PATHS:
        if exists(candidate):
            return candidate
    for pattern in WINDOWS_GLOBS:
        for candidate in sorted(globber(pattern), key=_windows_version_key,
                                reverse=True):
            if exists(candidate):
                return candidate
    raise KicadUnavailable(
        "kicad-cli not found. Install KiCad 8 or newer, then either put kicad-cli on "
        "your PATH or set KICAD_CLI to its full path.\n"
        "  macOS:   /Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli\n"
        "  Linux:   apt install kicad  (or the KiCad AppImage)\n"
        "  Windows: C:\\Program Files\\KiCad\\<version>\\bin\\kicad-cli.exe")


def probe_capability(cli: str, runner=subprocess.run) -> None:
    """Establish that this kicad-cli runs and supports every flag the gate passes.

    The exit status is checked first, and separately from the flags. Usage text
    from a command that failed is not evidence of anything — a broken wrapper
    can print a perfectly good help screen on its way out — and everything
    downstream leans on this having succeeded: run_drc treats a non-zero exit
    as a bad board rather than a bad install precisely because the probe is
    supposed to have ruled the install out already.

    Which stream carried the help text is not part of the requirement, so both
    are searched. Some tools print usage to stderr.
    """
    result = runner([cli, "pcb", "drc", "--help"], capture_output=True, text=True)
    returncode = getattr(result, "returncode", 0)
    help_text = ((getattr(result, "stdout", "") or "")
                 + (getattr(result, "stderr", "") or ""))
    if returncode != 0:
        raise KicadUnavailable(
            f"{cli} pcb drc --help failed (exit {returncode}), so the gate "
            "cannot establish what this kicad-cli supports. It said: "
            f"{help_text.strip()[:400] or '(nothing)'}")
    missing = [f for f in REQUIRED_FLAGS if f not in help_text]
    if missing:
        raise KicadUnavailable(
            f"{cli} does not support {', '.join(missing)}. The gate needs KiCad 8 or "
            "newer; upgrade KiCad or point KICAD_CLI at a newer install.")


def run_drc(cli: str, board: str, runner=subprocess.run, parity: bool = True):
    """One DRC pass: violations, unconnected items and parity together.

    Returns (drc, parity_error). parity_error is "" only when kicad-cli
    affirmed on stdout that the parity tests ran; otherwise it is whatever
    kicad-cli said about why, and the caller turns it into a blocking finding.
    Silence counts against the run rather than for it, because kicad-cli exits
    0 and writes an empty parity list either way. The violations in the same
    report are still valid, so this is not an environment failure.

    --refill-zones --save-board is intentional. A stale zone fill is the fault
    this gate exists to catch, and refilling without saving would leave the
    board on disk disagreeing with the package just exported from it.

    --exit-code-violations is deliberately not passed: the gate applies its
    own policy on the parsed JSON, so a non-zero exit here would only obscure
    a successful run that merely found problems.
    """
    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, "drc.json")
        cmd = [cli, "pcb", "drc", "--format", "json", "--severity-all"]
        if parity:
            cmd.append("--schematic-parity")
        cmd += ["--refill-zones", "--save-board", "-o", out, board]
        result = runner(cmd, capture_output=True, text=True)
        stderr = (getattr(result, "stderr", "") or "").strip()
        if getattr(result, "returncode", 0) != 0:
            raise BoardUnreadable(
                f"kicad-cli DRC failed (exit {result.returncode}): {stderr}")
        stdout = getattr(result, "stdout", "") or ""
        parity_error = ""
        if parity and not PARITY_RAN.search(stdout):
            # Report what kicad-cli said if it said anything. When it said
            # nothing at all the gate still may not call this a pass, so the
            # reason cannot be empty — a blank reason in the receipt would
            # read as "parity was fine".
            parity_error = stderr or (
                "kicad-cli did not report running the schematic parity tests "
                "and gave no reason. It exits 0 with an empty parity list "
                "whether the tests passed or never ran, so this cannot be "
                "read as a clean result.")
        with open(out) as fh:
            return json.load(fh), parity_error
