"""One-click KiCad 10 release: preflight → stage → gate → exports → scripts → publish → tag.

    python release.py <board.kicad_pcb> [--allow-dirty] [--no-tag]

Exit: 0 released · 2 gate blocked · 3 could not run / inputs changed ·
      4 refused (dirty, no rev, already released) · 5 an export or script failed
"""
import argparse
import datetime
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
# prefab-gate is vendored as prefab_gate/ (upstream scripts/ + KiCad 10 exclusions fix).
GATE = HERE / "prefab_gate" / "prefab_gate.py"
SCRIPTS = HERE / "scripts"
FAB_FILES = HERE / "fab"  # sheets shipped with the tool, e.g. the impedance control sheet


def _version():
    """This tool's PCM version: plugins/VERSION when installed, pcm/metadata.json in the repo."""
    try:
        return (HERE / "VERSION").read_text(encoding="utf-8").strip()
    except OSError:
        pass
    try:
        meta = json.loads((HERE / "pcm" / "metadata.json").read_text(encoding="utf-8"))
        return meta["versions"][0]["version"]
    except (OSError, ValueError, KeyError, IndexError):
        return "dev"


VERSION = _version()
# KiCad Plugin and Content Manager folder names
IBOM = "org_openscopeproject_InteractiveHtmlBom"

# Not allowed in a Windows folder name or a git tag. Whitespace too: "1 41" is a typo.
BAD_CHARS = re.compile(r'[<>:"/\\|?*~^\[\s]')


class Stop(Exception):
    """Ends the run with an exit code; the message is shown to the user."""

    def __init__(self, code, msg):
        super().__init__(msg)
        self.code = code


def read_rev(pcb_text):
    """The title-block rev of a .kicad_pcb's text, or "" when there is none."""
    m = re.search(r'\(title_block\b.*?\(rev "((?:[^"\\]|\\.)*)"\)', pcb_text, re.S)
    return m.group(1).strip() if m else ""


def expand_vars(text, pro):
    """KiCad text variables (${NAME}) from the .kicad_pro's text_variables. An undefined
    one -> Stop(4), so a folder is never named after the literal ${NAME}."""
    try:
        names = json.loads(Path(pro).read_text(encoding="utf-8-sig")).get("text_variables", {})
    except (OSError, ValueError):
        names = {}

    def sub(m):
        if m[1] not in names:
            where = ("Board Setup > Text Variables" if Path(pro).is_file()
                     else f"{Path(pro).name}, which is not beside the board")
            raise Stop(4, f"The PCB rev uses ${{{m[1]}}}, which is not defined in {where}.")
        return names[m[1]]
    return re.sub(r"\$\{([^}]+)\}", sub, text)


def board_rev(pcb, pcb_text):
    """(rev, slug) a release of this board is named by: the title-block rev with ${VARS}
    resolved from the .kicad_pro beside it, and its folder/tag-safe slug."""
    rev = expand_vars(read_rev(pcb_text), Path(pcb).with_suffix(".kicad_pro"))
    return rev, rev_slug(rev)


def release_target(pcb, cfg, slug, day):
    """(name, folder) a release publishes to. Stop(4) when the name pattern cannot name it.
    An existing folder or zip of that name is replaced (see heads_up). release() and the
    button's preview both use it, so the preview never promises a release that is then refused."""
    name = release_name(Path(pcb).stem, slug, day, cfg["name"])
    return name, Path(pcb).parent / cfg["folder"] / name  # "." = beside the board; absolute replaces it


def _local_drive(path):
    """True on a fixed local disk, where the Recycle Bin works (not network/removable/OneDrive-online)."""
    if sys.platform != "win32":
        return True
    import ctypes
    return ctypes.windll.kernel32.GetDriveTypeW(str(Path(path).resolve().anchor)) == 3  # DRIVE_FIXED


def heads_up(project, final, tag, head):
    """What a release to final, tagged tag, will do that the user should know first: replace an
    earlier release of the same name, or skip the tag because this rev is already tagged.
    Never refusals: the button shows them, release.py prints them, and goes on."""
    notes = []
    if final.exists() or final.with_name(f"{final.name}.zip").exists():
        notes.append(f"{final.name} already exists. Release replaces it; the old one goes "
                     "to the Recycle Bin."
                     + ("" if _local_drive(final) else " This is not a local drive: the old one is "
                        "deleted for good."))
    tagged = tag_commit(project, tag)
    if tagged and tagged == head:
        notes.append(f"{tag} is already on this commit: a re-run, released untagged.")
    elif tagged:
        notes.append(f"{tag} is on an older commit ({tagged[:7]}) and the board has changed "
                     "since. Bump the rev for a new version; else it is released untagged.")
    return notes


def tag_name(board, slug):
    return f"release/{board}/{slug}"


def rev_slug(rev):
    """'Rev 1.41' -> '1.41'. Refuses a rev that cannot name a folder and a git tag."""
    slug = re.sub(r"^rev(ision)?[\s._-]*", "", rev.strip(), flags=re.I)
    if not slug:
        raise Stop(4, "The PCB title block has no rev. Set it in Board Setup > Title Block, "
                      "commit, and rerun.")
    if BAD_CHARS.search(slug):
        raise Stop(4, f"rev {rev!r} has characters a folder or git tag cannot hold "
                      "(spaces or <>:\"/\\|?*~^[).")
    if ".." in slug or "@{" in slug or slug.startswith(".") or slug.endswith((".", ".lock")):
        raise Stop(4, f"rev {rev!r} cannot be a git tag (no \"..\", \"@{{\", leading or trailing \".\", "
                      "or \".lock\"). Change the rev in Board Setup > Title Block.")
    return slug


NAME = "{board}_{rev}_{date}"


def release_name(board, slug, day, fmt=NAME):
    """fmt takes {board}, {rev} (the slug) and {date} (DDMMYY)."""
    if BAD_CHARS.search(board):
        raise Stop(4, f"Board file name {board!r} has a space or one of <>:\"/\\|?*~^[. Rename the "
                      "board (and its .kicad_sch and .kicad_pro) without them.")
    try:
        name = fmt.format(board=board, rev=slug, date=f"{day:%d%m%y}")
    except (KeyError, IndexError, ValueError, AttributeError):
        name = ""
    if not name or BAD_CHARS.search(name):
        raise Stop(4, f"Release name pattern {fmt!r} must give a folder name from {{board}}, "
                      "{rev} and {date}, without spaces or <>:\"/\\|?*~^[.")
    return name


# What the release button lets you choose, per board, in <board>.release.json beside the board.
# The labels are the tick boxes in the button's dialog.
OUTPUTS = {
    "gerber": "Gerber + drill zip, PCB stackup",
    "position": "Position file (POS)",
    "component_data": "Component data CSV",
    "testpoints": "Test points",
    "bom": "BOM CSV",
    "ibom": "Interactive BOM (iBOM)",
    "schematic_pdf": "Schematic PDF",
    "schematic_pdf_a4": "Schematic PDF, fit to A4",
    "assembly_pdf": "Assembly PDF (silk + courtyard)",
    "step": "STEP 3D model",
    "render_top": "Render top",
    "render_bottom": "Render bottom",
    "render_iso": "Render isometric",
}
DEFAULTS = {
    "outputs": {k: k != "schematic_pdf_a4" for k in OUTPUTS},
    "bom_fields": "Reference,Value,Footprint,${QUANTITY},${DNP},MPN",
    "bom_labels": "Reference,Value,Footprint,Qty,DNP,MPN",
    "bom_group_by": "Value,Footprint,MPN,${DNP}",
    "bom_preset": "",  # a BOM preset saved in the schematic; when set, replaces the three above
    "folder": "Manufacturing",  # relative to the board ("." = beside it), or absolute
    "name": NAME,
    # copied as-is into 1_Fabrication every release, filled in by hand later; ";" between files.
    # A bare name is one shipped with the tool (FAB_FILES), so it works on every PC.
    "fab_extra": "Required_impedance_control.xlsx",
    # prefab-gate: strict = cosmetic findings block too; parity/ampacity = run those checks
    "gate": {"strict": False, "parity": True, "ampacity": True},
    # Claude Code reviews the schematic against its datasheets; the report lands in
    # 4_Documentation. Advice only: findings never block. Needs `claude` on PATH.
    "claude_review": False,
}


def config_path(project, board):
    return project / f"{board}.release.json"


def load_config(project, board):
    """DEFAULTS overlaid with <board>.release.json. Unknown keys or wrong types refuse (exit 4)
    rather than release something other than what was asked for."""
    cfg = json.loads(json.dumps(DEFAULTS))
    p = config_path(project, board)
    if not p.exists():
        return cfg
    try:
        user = json.loads(p.read_text(encoding="utf-8"))
    except ValueError as e:
        raise Stop(4, f"{p.name} is not valid JSON: {e}")

    def merge(into, new, where):
        if not isinstance(new, dict):
            raise Stop(4, f"{p.name}: {where or 'the file'} must be an object")
        for k, v in new.items():
            if k not in into:
                raise Stop(4, f"{p.name}: unknown setting {where}{k!r}. Known: {', '.join(into)}")
            if isinstance(into[k], dict):
                merge(into[k], v, f"{where}{k}.")
            elif type(v) is not type(into[k]):
                raise Stop(4, f"{p.name}: {where}{k} must be {type(into[k]).__name__}")
            else:
                into[k] = v
    outs = user.get("outputs") if isinstance(user, dict) else None
    if isinstance(outs, dict) and "renders" in outs:  # before 1.2.0 one tick for all three
        on = outs.pop("renders")
        outs.update({k: outs.get(k, on) for k in ("render_top", "render_bottom", "render_iso")})
    merge(cfg, user, "")
    return cfg


def save_config(project, board, cfg):
    config_path(project, board).write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")


STEP_TIMEOUT = 30 * 60  # seconds per command: a hung kicad-cli/iBOM/claude must not spin forever
run = subprocess.run  # tests swap this for a fake git / gate / kicad-cli / script runner


def sh(cmd, **kw):
    # no console window per kicad-cli/git call when started from the KiCad button
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    kw.setdefault("timeout", STEP_TIMEOUT)
    try:
        return run([str(c) for c in cmd], capture_output=True, text=True,
                   encoding="utf-8", errors="replace", creationflags=flags, **kw)
    except FileNotFoundError:
        raise Stop(3, f"{Path(str(cmd[0])).name} was not found. Install it or add it to PATH.")
    except subprocess.TimeoutExpired:
        raise Stop(5, f"{Path(str(cmd[0])).name} ran for over {kw['timeout'] // 60} minutes and "
                      "was stopped (hung?). Nothing published.")


def git(project, *args):
    return sh(["git", "-C", project, *args])


def own_output(project, rel, board, fmt=NAME):
    """git globs for this board's release folders and zips in rel, so a release made beside
    the board never counts as uncommitted on the next run. [] when rel is outside project."""
    try:
        sub = rel.resolve().relative_to(project.resolve()).as_posix()
        n = fmt.format(board=board, rev="*", date="[0-9]" * 6)
    except (ValueError, KeyError, IndexError):
        return []
    pre = "" if sub == "." else f"{sub}/"
    # a folder needs "/**" in a glob pathspec
    return [pre + p for p in (f"{n}/**", f"{n}.zip", f".{n}.partial/**", f".{n}.zip.partial")]


def git_state(project, ignore=()):
    """(HEAD commit, uncommitted lines under project). Commit is None outside a git repo.
    ignore: extra globs (relative to project) that never count, e.g. own_output()."""
    head = git(project, "rev-parse", "HEAD")
    if head.returncode:
        if "dubious ownership" in head.stderr or "safe.directory" in head.stderr:
            raise Stop(4, f"git refused this folder: {head.stderr.strip().splitlines()[0]}\n"
                          f"Run: git config --global --add safe.directory {project}")
        return None, []
    # .kicad_prl is local view state KiCad rewrites on open/close; never a release input.
    # *.release.json is saved by the button on every run; the manifest records what was used.
    status = git(project, "status", "--porcelain", "--", ".", ":(exclude,glob)**/*.kicad_prl",
                 ":(exclude,glob)**/*.release.json", *(f":(exclude,glob){g}" for g in ignore))
    return head.stdout.strip(), [line for line in status.stdout.splitlines() if line.strip()]


def tag_commit(project, tag):
    """The commit tag points at, or None when there is no such tag."""
    r = git(project, "rev-parse", "-q", "--verify", f"refs/tags/{tag}^{{commit}}")
    return r.stdout.strip() if r.returncode == 0 else None


def trash(path):
    """Send a file or folder to the Recycle Bin (Windows), so a replaced release can be got
    back. Stop(5) when it cannot, e.g. a file in it is open."""
    if not path.exists():
        return
    import ctypes
    from ctypes import wintypes

    class OP(ctypes.Structure):  # SHFILEOPSTRUCTW
        _fields_ = [("hwnd", wintypes.HWND), ("wFunc", ctypes.c_uint), ("pFrom", wintypes.LPCWSTR),
                    ("pTo", wintypes.LPCWSTR), ("fFlags", ctypes.c_ushort), ("aborted", wintypes.BOOL),
                    ("maps", ctypes.c_void_p), ("title", wintypes.LPCWSTR)]
    # FO_DELETE; FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI
    op = OP(None, 3, str(path.resolve()) + "\0", None, 0x40 | 0x10 | 0x4 | 0x400, False, None, None)
    if ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op)) or path.exists():
        raise Stop(5, f"Could not move the earlier {path.name} to the Recycle Bin (is a file in "
                      "it open?). Nothing published.")


STAGE_GLOBS = ("*.kicad_pcb", "*.kicad_sch", "*.kicad_pro", "*.kicad_dru",
               "fp-lib-table", "sym-lib-table", "ibom.config.ini")


def _skipped(rel_dir):
    return any(part == "release" or part.endswith("-backups") for part in rel_dir.parts)


def stage(project, dest):
    """Copy what the exports need into dest; hierarchical sub-sheets keep their relative paths."""
    files = {p for g in STAGE_GLOBS for p in project.glob(g)}
    files |= {p for p in project.rglob("*.kicad_sch")
              if not _skipped(p.relative_to(project).parent)}
    for src in sorted(files):
        dst = dest / src.relative_to(project)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


ASSEMBLY = "_assembly_copy"  # in the stage: the board the assembly PDF plots (references shown)


def hash_inputs(root):
    """sha256 of every .kicad_pcb/.kicad_sch under root, keyed by posix relative path. The
    assembly PDF's own derived copy is not an input."""
    return {p.relative_to(root).as_posix(): sha256(p) for p in sorted(root.rglob("*"))
            if p.suffix in (".kicad_pcb", ".kicad_sch") and ASSEMBLY not in p.relative_to(root).parts}


def show_references(pcb_text):
    """The board text with every footprint Reference visible. Dense boards hide them on the
    silk, but an assembly drawing needs them; only ever written to the assembly PDF's copy."""
    out, last = [], 0
    for m in re.finditer(r'\(property "Reference" ', pcb_text):
        depth, i = 0, m.start()
        while True:  # to the property's closing paren; parens inside quoted strings don't count
            c = pcb_text[i]
            if c == '"':
                i += 1
                while pcb_text[i] != '"':
                    i += 2 if pcb_text[i] == "\\" else 1
            elif c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
                if not depth:
                    break
            i += 1
        out += [pcb_text[last:m.start()], re.sub(r"\s*\(hide yes\)", "", pcb_text[m.start():i + 1])]
        last = i + 1
    return "".join(out) + pcb_text[last:]


def _documents():
    """The user's Documents folder as Windows has it. It may be redirected to
    another drive or to OneDrive, so ~/Documents is not enough."""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders") as k:
            return Path(os.path.expandvars(winreg.QueryValueEx(k, "Personal")[0]))
    except (ImportError, OSError):
        return Path.home() / "Documents"


def plugins_dir(env=None, documents=_documents, here=HERE):
    """KiCad 10's Plugin and Content Manager plugins folder holding iBOM.
    Installed as a PCM package we sit in that folder ourselves, so here.parent comes first."""
    env = os.environ if env is None else env
    third = env.get("KICAD10_3RD_PARTY")
    candidates = ([Path(third) / "plugins"] if third else []) + [
        here.parent, documents() / "KiCad" / "10.0" / "3rdparty" / "plugins"]
    for plugins in candidates:
        if (plugins / IBOM).is_dir():
            return plugins
    raise Stop(3, f"Install {IBOM} from KiCad's Plugin and Content Manager "
                  "(or set KICAD10_3RD_PARTY). Looked in:\n" + "\n".join(map(str, candidates)))


def find_tools(ibom=True):
    """(kicad-cli, KiCad's python.exe, plugins dir or None). kicad-cli via prefab-gate's
    locator so the gate and the release run the same KiCad. The plugins dir (iBOM) is only
    looked up when iBOM is ticked."""
    if not GATE.exists():
        raise Stop(3, f"prefab-gate missing at {GATE.parent}; reinstall Fab Release.")
    sys.path.insert(0, str(GATE.parent))
    from gate.kicad import KicadUnavailable, locate_cli
    try:
        cli = Path(locate_cli())
    except KicadUnavailable as e:
        raise Stop(3, str(e))
    py = cli.with_name("python.exe")
    if not py.exists():
        raise Stop(3, f"KiCad's python.exe not found beside {cli}")
    return cli, py, plugins_dir() if ibom else None


def find_claude():
    claude = shutil.which("claude")
    if not claude:
        raise Stop(3, "Claude review is ticked but Claude Code (claude) is not on PATH. "
                      "Install it from claude.com/claude-code, or untick Claude review.")
    return claude


REVIEW = "schematic review (Claude)"
# Headless runs cannot ask permission: only what the review needs. No other shell, and Write
# can only add the report; an edit to a staged input fails the release's hash check.
REVIEW_TOOLS = "Read Glob Grep Write WebSearch WebFetch Bash(python:*) Bash(py:*)"


def review_report(st, board):
    return st / "review" / f"{board}_schematic_review.md"


def review_command(claude, st, board):
    """claude -p running skills/schematic-review on the staged schematic; cwd must be st."""
    skill = HERE / "skills" / "schematic-review" / "SKILL.md"
    prompt = (f"Review the KiCad schematic {st / board}.kicad_sch by following {skill}. "
              f"${{CLAUDE_PLUGIN_ROOT}} there means {HERE}. Use {st / 'review'} as the facts "
              f"folder and write the report to {review_report(st, board)} (a new file, do not "
              "ask). Change no other file. Reply with one line: the counts of likely bugs, "
              "checks and nits.")
    return [claude, "-p", prompt, "--allowedTools", REVIEW_TOOLS, "--add-dir", HERE]


def step(label, cmd, expect=(), **kw):
    """Run one command. It failed if it exits non-zero OR leaves an expected file missing:
    kicad-cli and the fab-data script can both exit 0 having written nothing."""
    print(f"  {label}", flush=True)
    r = sh(cmd, **kw)
    missing = [str(p) for p in expect if not Path(p).exists()]
    if not (r.returncode or missing):
        note = re.search(r"(?i)^.*(could not add 3d model|3d model.*not found).*$", r.stdout + r.stderr, re.M)
        if note:  # kicad-cli exits 0 with a STEP/render lacking those parts
            print(f"NOTE: {label}: some 3D models are missing, those parts are boxes or absent. "
                  f"{note[0].strip()}", flush=True)
    if r.returncode or missing:
        why = (f"it exited {r.returncode}" if r.returncode else "it exited 0 but wrote nothing")
        raise Stop(5, f"{label} failed: {why}"
                      + (f"; expected file not made: {', '.join(Path(p).name for p in missing)}"
                         if missing else "")
                      + f"\n{r.stdout}{r.stderr}")


# What a fab needs besides every copper layer; names as in the board's (layers ...) header.
FAB_LAYERS = ("F.Mask", "B.Mask", "F.Paste", "B.Paste", "F.SilkS", "B.SilkS", "Edge.Cuts")
_SIDED = {("Soldermask", "Top"): "F.Mask", ("Soldermask", "Bot"): "B.Mask",
          ("Legend", "Top"): "F.SilkS", ("Legend", "Bot"): "B.SilkS",
          ("Paste", "Top"): "F.Paste", ("Paste", "Bot"): "B.Paste"}
_FUNCTION = re.compile(r"TF\.FileFunction,(\w+),?(\w*),?(\w*)")


def board_layers(pcb_text):
    """(id, name) of every layer the board defines, from its top-level (layers ...) header."""
    block = re.search(r"^\t\(layers\n(.*?)^\t\)", pcb_text, re.M | re.S)
    return [(int(i), n) for i, n in re.findall(r'\((\d+) "([^"]+)"', block[1])] if block else []


def copper_layers(pcb_text):
    """Copper layer names top to bottom: F.Cu, In1.Cu, ..., B.Cu (Gerber L1..Ln)."""
    return [n for _, n in board_layers(pcb_text) if n.endswith(".Cu")]


def unplotted_layers(pcb_text):
    """Fab layers (copper, masks, silks, outline) that File > Plot is not set to output: its
    layerselection is a bit mask over the same layer ids the (layers ...) header uses."""
    m = re.search(r"\(layerselection 0x([0-9a-fA-F_]+)\)", pcb_text)
    sel = int(m[1].replace("_", ""), 16) if m else 0
    return [n for i, n in board_layers(pcb_text)
            if (n.endswith(".Cu") or n in FAB_LAYERS) and not sel >> i & 1]


def with_fab_layers(pcb_text):
    """The board text with every copper layer, mask, silk and the outline added to File > Plot's
    layer selection, so a release never depends on what the Plot dialog was left at. Only ever
    applied to the staged copy."""
    m = re.search(r"\(layerselection 0x([0-9a-fA-F_]+)\)", pcb_text)
    if not m:
        return pcb_text
    sel = int(m[1].replace("_", ""), 16)
    for i, n in board_layers(pcb_text):
        if n.endswith(".Cu") or n in FAB_LAYERS:
            sel |= 1 << i
    return pcb_text.replace(m[0], f"(layerselection 0x{sel:032x})", 1)


def check_gerbers(fab, pcb_text):
    """Stop unless the fab files hold every copper layer of the board, both masks and silks,
    the outline and the plated drill. Read from each file's own FileFunction attribute (X2, or
    X1's "G04 #@!" comment), so nothing slips through whatever File > Plot was set to."""
    copper, found = copper_layers(pcb_text), set()
    for f in Path(fab).iterdir():
        with open(f, encoding="utf-8", errors="replace") as fh:
            m = _FUNCTION.search(fh.read(4096))  # attributes sit in the header
        if not m:
            continue
        kind, arg, side = m[1], m[2], m[3]
        if kind == "Copper" and side in ("Top", "Bot"):
            found.add("F.Cu" if side == "Top" else "B.Cu")
        elif kind == "Copper" and arg[1:].isdigit() and 0 < int(arg[1:]) <= len(copper):
            found.add(copper[int(arg[1:]) - 1])
        found.add({"Profile": "Edge.Cuts", "Plated": "plated drill"}.get(kind) or _SIDED.get((kind, arg)))
    plated = re.search(r"\(via[\s(]|thru_hole", pcb_text)  # an SMD-only board has no plated drill file
    missing = [n for n in [*copper, *FAB_LAYERS, *(["plated drill"] if plated else [])] if n not in found]
    if missing:
        raise Stop(5, f"Gerbers: missing {', '.join(missing)}. kicad-cli did not write them; "
                      "the board has no such layer or its Plot settings are damaged.")


def _blocked(stdout):
    """The gate's JSON verdict as a short list a person can act on. stderr is left out: its
    zone-refill note names the temp copy, not the user's board."""
    try:
        v = json.loads(stdout)
    except ValueError:
        return f"prefab-gate BLOCKED. Fix the board, commit, rerun.\n{stdout}"
    lines = [f"prefab-gate BLOCKED: {len(v.get('blocking', []))} blocking, "
             f"{len(v.get('cosmetic', []))} cosmetic. Fix the items below (DRC: Inspect > Design Rules "
             "Checker; also schematic parity and trace width), commit, rerun."]
    for f in v.get("blocking", []):
        lines.append(f"\n- {f.get('description', f.get('type', '?'))}")
        if f.get("reason"):
            lines.append(f"    why: {f['reason']}")
        items = f.get("items", [])
        lines += [f"    {item}" for item in items[:20]]
        if len(items) > 20:
            lines.append(f"    ... and {len(items) - 20} more (run DRC in KiCad for the full list)")
    return "\n".join(lines)


def run_gate(pcb, cli, gate=DEFAULTS["gate"]):
    """prefab-gate check on the staged board -> verdict dict."""
    print("  prefab-gate check", flush=True)
    flags = ((["--strict"] if gate["strict"] else []) + ([] if gate["parity"] else ["--no-parity"])
             + ([] if gate["ampacity"] else ["--no-ampacity"]))
    # PYTHONUTF8: kicad-cli speaks UTF-8; the gate would decode it as cp1252 and choke on some names
    r = sh([sys.executable, GATE, "check", "--json", *flags, pcb],
           env={**os.environ, "KICAD_CLI": str(cli), "PYTHONUTF8": "1"})
    if r.returncode == 2:
        raise Stop(2, _blocked(r.stdout))
    if r.returncode:
        out = r.stdout + r.stderr
        why = ("kicad-cli wrote no DRC report; check the KiCad install." if "drc.json" in out
               else "prefab-gate crashed (internal error)." if "Traceback" in out
               else "prefab-gate could not run.")
        raise Stop(3, f"{why} (exit {r.returncode})\n{out}")
    try:
        return json.loads(r.stdout)
    except ValueError:
        return {"raw": r.stdout}


def outputs(out, board):
    """Where the exports and the test-point plugin write inside the stage's out/ folder."""
    fab = out / "fab"
    return {
        "gerber_job": fab / f"{board}-job.gbrjob",
        "drill_report": fab / f"{board}-drl.rpt",
        "pos": out / f"{board}-all-pos.csv",
        "bom": out / f"{board}-bom.csv",
        "sch_pdf": out / f"{board}.pdf",
        "sch_a4": out / f"{board}_A4.pdf",
        "assembly_top": out / f"{board}_assembly_top.pdf",
        "assembly_bottom": out / f"{board}_assembly_bottom.pdf",
        "assembly_bottom_mirrored": out / f"{board}_assembly_bottom_mirrored.pdf",
        "step": out / f"{board}.step",
        "png_top": out / f"{board}_F.png",
        "png_bottom": out / f"{board}_B.png",
        "png_iso": out / f"{board}_iso.png",
        "testpoints": out / f"{board}-testpoints.csv",
    }


A4_JOBSET = "_a4.kicad_jobset"  # in the stage, written by build()


def a4_jobset(out, board):
    """A one-job KiCad jobset: the schematic PDF scaled to A4, KiCad's own plot (links kept).
    page_size "A4" verified on kicad-cli 10.0.7; other values silently plot as drawn."""
    return {"jobs": [{"id": "a4a4a4a4-0000-4000-8000-000000000001", "description": "",
                      "type": "sch_export_plot_pdf",
                      "settings": {"page_size": "A4", "output_filename": f"{board}_A4.pdf"}}],
            "meta": {"version": 1},
            "outputs": [{"id": "a4a4a4a4-0000-4000-8000-000000000002", "description": "", "only": [],
                         "type": "folder", "settings": {"output_path": str(out)}}]}


def export_commands(cli, st, board, cfg=DEFAULTS):
    """(output key, label, kicad-cli command, expected files). Flags verified on a real board,
    kicad-cli 10.0.7."""
    pcb, sch, o = st / f"{board}.kicad_pcb", st / f"{board}.kicad_sch", outputs(st / "out", board)
    asm = st / ASSEMBLY / f"{board}.kicad_pcb"
    asm_pdf = [cli, "pcb", "export", "pdf", "--mode-single", "--scale", "0", "--black-and-white",
               "--exclude-value"]  # values plot even with only silk + courtyard selected
    fab = f"{st / 'out' / 'fab'}/"  # trailing slash: -o is a directory
    # Drill files share the Gerbers' origin, which File > Plot sets ("Use drill/place file origin")
    origin = "plot" if "(useauxorigin yes)" in (pcb.read_text(encoding="utf-8-sig")
                                                if pcb.is_file() else "") else "absolute"
    return [
        ("gerber", "gerbers", [cli, "pcb", "export", "gerbers", "--board-plot-params", "-o", fab, pcb],
         [o["gerber_job"]]),  # layer files' extension follows File > Plot (.gbr or Protel .gtl); check_gerbers reads them
        ("gerber", "drill", [cli, "pcb", "export", "drill", "--drill-origin", origin, "--excellon-separate-th", "--generate-map",
                   "--map-format", "gerberx2", "--generate-report", "--report-path", o["drill_report"],
                   "-o", fab, pcb], [o["drill_report"]]),
        ("position", "position", [cli, "pcb", "export", "pos", "--format", "csv", "--units", "mm",
                      "--side", "both", *(["--use-drill-file-origin"] if origin == "plot" else []),
                      "-o", o["pos"], pcb], [o["pos"]]),
        ("bom", "BOM", [cli, "sch", "export", "bom",
                        *(["--preset", cfg["bom_preset"]] if cfg["bom_preset"] else
                          ["--fields", cfg["bom_fields"], "--labels", cfg["bom_labels"],
                           "--group-by", cfg["bom_group_by"]]),
                        "-o", o["bom"], sch], [o["bom"]]),
        ("schematic_pdf", "schematic PDF", [cli, "sch", "export", "pdf", "-o", o["sch_pdf"], sch], [o["sch_pdf"]]),
        # sch export pdf has no page size; a jobset's PDF job does (File > Plot's "Page size")
        ("schematic_pdf_a4", "schematic PDF A4", [cli, "jobset", "run", "--stop-on-error", "-f",
                                                  st / A4_JOBSET, st / f"{board}.kicad_pro"], [o["sch_a4"]]),
        # silk + courtyard + outline per side, fit to the page, black and white for printing;
        # bottom both seen through the board from the top, as the hand-made drawings, and
        # mirrored, as seen holding the board bottom up
        ("assembly_pdf", "assembly PDF top", [*asm_pdf, "--layers", "F.SilkS,F.CrtYd,Edge.Cuts",
                                              "-o", o["assembly_top"], asm], [o["assembly_top"]]),
        ("assembly_pdf", "assembly PDF bottom", [*asm_pdf, "--layers", "B.SilkS,B.CrtYd,Edge.Cuts",
                                                 "-o", o["assembly_bottom"], asm], [o["assembly_bottom"]]),
        ("assembly_pdf", "assembly PDF bottom mirrored", [*asm_pdf, "--mirror", "--layers",
                                                          "B.SilkS,B.CrtYd,Edge.Cuts",
                                                          "-o", o["assembly_bottom_mirrored"], asm],
         [o["assembly_bottom_mirrored"]]),
        ("step", "STEP", [cli, "pcb", "export", "step", "--subst-models", "-f", "-o", o["step"], pcb],
         [o["step"]]),
        ("render_top", "render top", [cli, "pcb", "render", "--side", "top", "--quality", "high",
                        "-o", o["png_top"], pcb], [o["png_top"]]),
        ("render_bottom", "render bottom", [cli, "pcb", "render", "--side", "bottom", "--quality", "high",
                           "-o", o["png_bottom"], pcb], [o["png_bottom"]]),
        ("render_iso", "render isometric", [cli, "pcb", "render", "--rotate", "-45,0,45", "--quality",
                              "high", "-o", o["png_iso"], pcb], [o["png_iso"]]),
    ]


def script_commands(py, plugins, st, board):
    """(output key, label, command, expected files). Stackup, component data and test points
    come from scripts/fab_exports.py, which reads the board file itself (no pcbnew/SWIG), so
    any Python runs it; iBOM still needs KiCad's python."""
    pcb, o = st / f"{board}.kicad_pcb", outputs(st / "out", board)
    fab = [sys.executable, SCRIPTS / "fab_exports.py"]
    stack, comp = st / "gerber" / f"{board}_pcbstack.txt", st / "ASSAY" / "pcb_component_data.csv"
    return [
        ("gerber", "PCB stackup", [*fab, "stackup", pcb, stack], [stack]),
        ("component_data", "component data", [*fab, "components", pcb, comp], [comp]),
        ("testpoints", "test points", [*fab, "testpoints", pcb, o["testpoints"]], [o["testpoints"]]),
    ] + ([] if plugins is None else [  # None: iBOM unticked, plugin not looked up
        ("ibom", "iBOM", [py, plugins / IBOM / "generate_interactive_bom.py", "--no-browser",
                  "--dest-dir", st / "ibom", "--name-format", "ibom_%f_%D_%T",
                  # MPN and KiCad's own DNP flag as columns (MPN/mpn merged); DNP parts stay in
                  # the table, grouped apart with "DNP" in that column
                  "--show-fields", "Value,Footprint,MPN,kicad_dnp",
                  "--group-fields", "Value,Footprint,MPN,kicad_dnp", "--normalize-field-case", pcb], []),
    ])


def planned_steps(board, cfg=DEFAULTS):
    """The step labels a release prints, in order: the button's progress window counts them."""
    st, plugins = Path("stage"), (Path("plugins") if cfg["outputs"]["ibom"] else None)
    cmds = (export_commands(Path("kicad-cli"), st, board, cfg)
            + script_commands(Path("python"), plugins, st, board))
    return (["prefab-gate check"] + [label for key, label, _, _ in cmds if cfg["outputs"][key]]
            + [REVIEW] * cfg["claude_review"])


# Release folder layout, by who receives it: fab house, assembly house, test jig, everyone.
FAB, ASM, TEST, DOC = "1_Fabrication", "2_Assembly", "3_Test", "4_Documentation"
# Which folder each output lands in; the button's dialog groups its tick boxes the same way.
GROUPS = ((FAB, ("gerber",)),
          (ASM, ("position", "bom", "ibom", "component_data", "assembly_pdf")),
          (TEST, ("testpoints",)),
          (DOC, ("schematic_pdf", "schematic_pdf_a4", "step", "render_top", "render_bottom", "render_iso")))


def zip_files(dest, files):
    """Write (arcname, path) pairs into a new zip at dest."""
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
        for arc, path in files:
            z.write(path, arc)


def extra_files(cfg):
    return [FAB_FILES / f.strip() for f in cfg["fab_extra"].split(";") if f.strip()]  # absolute stays


def build(st, partial, board, name, cli, py, plugins, cfg=DEFAULTS, claude=None):
    """The ticked exports, scripts and plugins inside the stage st, then the release layout in
    partial."""
    on, o = cfg["outputs"], outputs(st / "out", board)
    (st / "out" / "fab").mkdir(parents=True)
    for d in ("gerber", "ASSAY"):
        (st / d).mkdir()
    if on["assembly_pdf"]:  # its own copy: the gate-checked staged board is never edited
        (st / ASSEMBLY).mkdir()
        text = (st / f"{board}.kicad_pcb").read_text(encoding="utf-8-sig")
        (st / ASSEMBLY / f"{board}.kicad_pcb").write_text(show_references(text), encoding="utf-8")
        if (st / f"{board}.kicad_pro").exists():  # text variables, title block
            shutil.copy2(st / f"{board}.kicad_pro", st / ASSEMBLY)
    if on["schematic_pdf_a4"]:
        (st / A4_JOBSET).write_text(json.dumps(a4_jobset(st / "out", board)), encoding="utf-8")
    for key, label, cmd, expect in (export_commands(cli, st, board, cfg)
                                    + script_commands(py, plugins, st, board)):
        if on[key]:
            step(label, cmd, expect)
    if on["gerber"]:
        check_gerbers(st / "out" / "fab", (st / f"{board}.kicad_pcb").read_text(encoding="utf-8-sig"))
    if claude:  # last: minutes long, so only once everything else has passed
        try:
            step(REVIEW, review_command(claude, st, board), [review_report(st, board)], cwd=st)
        except Stop as e:  # advice only: never costs the release
            print(f"NOTE: Claude review failed, released without the report (run `claude` once "
                  f"to sign in): {str(e).splitlines()[0]}", flush=True)
            claude = None

    def put(src, folder, as_name=None):
        (partial / folder).mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, partial / folder / (as_name or src.name))

    folder = {key: f for f, keys in GROUPS for key in keys}
    partial.mkdir(parents=True)
    if on["gerber"]:
        stack = st / "gerber" / f"{board}_pcbstack.txt"
        put(stack, folder["gerber"])
        fab = sorted(p for p in (st / "out" / "fab").iterdir() if p.is_file())
        zip_files(partial / folder["gerber"] / f"{board}.zip",
                  [(p.name, p) for p in fab] + [(stack.name, stack)])
    for key, src in (("position", o["pos"]),
                     ("component_data", st / "ASSAY" / "pcb_component_data.csv"),
                     ("assembly_pdf", o["assembly_top"]), ("assembly_pdf", o["assembly_bottom"]),
                     ("assembly_pdf", o["assembly_bottom_mirrored"]),
                     ("testpoints", o["testpoints"]),
                     ("schematic_pdf", o["sch_pdf"]), ("schematic_pdf_a4", o["sch_a4"]),
                     ("render_top", o["png_top"]), ("render_bottom", o["png_bottom"]),
                     ("render_iso", o["png_iso"])):
        if on[key]:
            put(src, folder[key])
    for f in extra_files(cfg):
        put(f, FAB)
    if on["bom"]:
        put(o["bom"], folder["bom"], f"{name}_BOM.csv")
    if on["ibom"]:
        ibom = sorted((st / "ibom").glob(f"ibom_{board}_*.html"))
        if len(ibom) != 1:
            raise Stop(5, f"iBOM wrote {len(ibom)} ibom_{board}_*.html files, expected 1")
        put(ibom[0], folder["ibom"])
    if on["step"]:
        (partial / folder["step"]).mkdir(exist_ok=True)
        zip_files(partial / folder["step"] / f"{name}.zip", [(o["step"].name, o["step"])])
    if claude:
        put(review_report(st, board), DOC)


def release(pcb, allow_dirty=False, tag=True, today=None):
    """Release one board; returns the published folder. Raises Stop with the exit code."""
    pcb = Path(pcb).resolve()
    project, board = pcb.parent, pcb.stem
    if pcb.suffix != ".kicad_pcb" or not pcb.is_file():
        raise Stop(3, f"No board at {pcb}")
    if not (project / f"{board}.kicad_sch").is_file():
        raise Stop(4, f"{board}.kicad_sch must sit beside the board (gate parity and the BOM need it).")
    if not (project / f"{board}.kicad_pro").is_file():
        raise Stop(4, f"{board}.kicad_pro must sit beside the board (net classes, DRC rules and text "
                      "variables are read from it).")
    cfg = load_config(project, board)
    if not any(cfg["outputs"].values()):
        raise Stop(4, "No outputs are ticked. Tick at least one.")
    cli, py, plugins = find_tools(ibom=cfg["outputs"]["ibom"])
    claude = find_claude() if cfg["claude_review"] else None
    missing = [f for f in extra_files(cfg) if not f.is_file()]
    if missing:
        raise Stop(3, "Extra fab file not found (BOM, name, prefab-gate tab):\n"
                      + "\n".join(map(str, missing)))

    rel = project / cfg["folder"]  # "." = beside the board; an absolute folder replaces project
    commit, dirty = git_state(project, own_output(project, rel, board, cfg["name"]))
    if commit is None and not allow_dirty:
        raise Stop(4, f"{project} is not in a git repo with a commit. Commit it, "
                      "or use --allow-dirty for a trial run.")
    if dirty and not allow_dirty:
        raise Stop(4, "Uncommitted changes. Commit them first, "
                      "or use --allow-dirty for a trial run:\n" + "\n".join(dirty))
    rev, slug = board_rev(pcb, pcb.read_text(encoding="utf-8-sig"))
    day = today or datetime.date.today()
    name, final = release_target(pcb, cfg, slug, day)
    partial = rel / f".{name}.partial"
    zip_final, zip_partial = rel / f"{name}.zip", rel / f".{name}.zip.partial"
    tag_ref = tag_name(board, slug)
    for note in heads_up(project, final, tag_ref, commit):
        print(f"NOTE: {note}")
    earlier = tag_commit(project, tag_ref)  # this rev was released before: never move its tag
    do_tag = tag and commit is not None and not allow_dirty and not earlier  # dirty matches no commit

    cli_version = sh([cli, "version"]).stdout.strip()
    if re.match(r"\d+", cli_version) and not cli_version.startswith("10."):
        raise Stop(3, f"kicad-cli at {cli} is KiCad {cli_version}; Fab Release needs KiCad 10. "
                      "Set KICAD_CLI to KiCad 10's bin\\kicad-cli.exe.")
    source_sha = sha256(pcb)
    try:
        rel.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise Stop(4, f"Release folder {rel} cannot be created: {e}")
    if cfg["outputs"]["gerber"] and "(stackup" not in pcb.read_text(encoding="utf-8-sig"):
        print("NOTE: no Physical Stackup saved in the board; the stackup file lists no copper or "
              "dielectric. Set it in Board Setup > Physical Stackup.", flush=True)
    ignore = rel / ".gitignore"  # release output never shows as uncommitted
    # only in a subfolder of the board's folder, and never over an existing one
    if rel.resolve() != project and rel.resolve().is_relative_to(project) and not ignore.exists():
        ignore.write_text("*\n")
    shutil.rmtree(partial, ignore_errors=True)  # left by a crashed run
    zip_partial.unlink(missing_ok=True)
    with tempfile.TemporaryDirectory(prefix="fab_release_", ignore_cleanup_errors=True) as tmp:
        st = Path(tmp)
        stage(project, st)
        staged = st / pcb.name
        staged.write_text(with_fab_layers(staged.read_text(encoding="utf-8-sig")), encoding="utf-8")
        verdict = run_gate(st / pcb.name, cli, cfg["gate"])
        checked = hash_inputs(st)  # after the gate's zone refill, the last write allowed
        try:
            build(st, partial, board, name, cli, py, plugins, cfg, claude)
            if hash_inputs(st) != checked:
                raise Stop(3, "A staged board/schematic changed after the gate checked it. "
                              "Nothing published.")
            files = sorted(p for p in partial.rglob("*") if p.is_file())
            manifest = {
                "board": board, "rev": rev, "release": name, "date": day.isoformat(),
                "git_commit": commit, "dirty": commit is None or bool(dirty),
                "kicad_cli": cli_version, "tool_version": VERSION,
                "source_board_sha256": source_sha,
                "staged_sha256": checked,
                "staged_note": "staged copy after prefab-gate zone refill; "
                               "differs from the source when fills changed",
                "gate": verdict,
                "rerelease_of": {"tag": tag_ref, "commit": earlier} if earlier else None,
                "config": cfg,
                "files": {p.relative_to(partial).as_posix(): sha256(p) for p in files},
            }
            (partial / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
            zip_files(zip_partial, [(f"{name}/{p.relative_to(partial).as_posix()}", p)
                                    for p in sorted(partial.rglob("*")) if p.is_file()])
            for old in (final, zip_final):  # locked? say so before the earlier release is binned
                if old.exists():
                    try:
                        os.rename(old, old)
                    except OSError as e:
                        raise Stop(5, f"Cannot replace {old.name}: {e}. Close it (Explorer, mail, "
                                      "OneDrive, antivirus) and release again. Nothing published.")
            trash(final)  # an earlier release of the same name (heads_up said so)
            trash(zip_final)
            partial.rename(final)  # the commit point: before this nothing looks released
            zip_partial.rename(zip_final)
        except BaseException:
            shutil.rmtree(partial, ignore_errors=True)
            zip_partial.unlink(missing_ok=True)
            raise
    if do_tag:
        r = git(project, "tag", tag_ref, commit)
        if r.returncode:
            raise Stop(5, f"Released {final}, but git tag failed:\n{r.stderr}")
    return final


class _Args(argparse.ArgumentParser):
    def error(self, message):  # 2 means "gate blocked", so usage errors are 3
        self.exit(3, f"{self.prog}: error: {message}\n")


def main(argv=None):
    ap = _Args(prog="release.py",
               description="One-click KiCad 10 release (prefab-gate + kicad-cli + fab-data script).")
    ap.add_argument("board", nargs="?", help="path to <board>.kicad_pcb")
    ap.add_argument("--allow-dirty", action="store_true",
                    help="trial run: allow uncommitted changes or no git repo; never tags")
    ap.add_argument("--no-tag", action="store_true", help="do not create the git tag")
    a = ap.parse_args(argv)
    if not a.board:
        ap.error("give a board")
    try:
        print(f"RELEASED {release(a.board, a.allow_dirty, not a.no_tag)}")
        return 0
    except Stop as e:
        print(f"STOP: {e}", file=sys.stderr)
        return e.code
    except OSError as e:  # disk full, read-only or locked output folder: say so, not a traceback
        print(f"STOP: Could not write {e.filename or 'a release file'} ({e.strerror or e}). The folder "
              "is read-only, the disk is full, or a file is held by OneDrive or antivirus. Nothing "
              "was published.", file=sys.stderr)
        return 5


if __name__ == "__main__":
    sys.exit(main())
