"""Component data, test points and PCB stackup straight from the .kicad_pcb text: no pcbnew
(SWIG), any Python 3.9+. Replaces older SWIG fab-data scripts and the TheJigsApp
kicad-testpoints plugin in a release; same columns and layout.

    python fab_exports.py components|testpoints|stackup <board.kicad_pcb> <out file>
"""
import csv
import json
import math
import re
import sys
from fnmatch import fnmatchcase
from pathlib import Path

_TOKEN = re.compile(r'\s*(?:(\()|(\))|"((?:[^"\\]|\\.)*)"|([^\s()"]+))')
_ESCAPES = {"n": "\n", "t": "\t", "r": "\r"}


def parse(text):
    """S-expression -> nested lists of str. Quoted and bare atoms both become str."""
    stack, cur, pos = [], [], 0
    end = len(text.rstrip())
    while pos < end:
        m = _TOKEN.match(text, pos)
        if not m:
            raise ValueError(f"bad s-expression at offset {pos}")
        pos = m.end()
        if m.group(1):
            stack.append(cur)
            cur = []
        elif m.group(2):
            done, cur = cur, stack.pop()
            cur.append(done)
        elif m.group(3) is not None:
            cur.append(re.sub(r"\\(.)", lambda e: _ESCAPES.get(e[1], e[1]), m.group(3)))
        else:
            cur.append(m.group(4))
    return cur[0]


def kids(node, name):
    return [c for c in node[1:] if isinstance(c, list) and c and c[0] == name]


def kid(node, name, default=None):
    found = kids(node, name)
    return found[0] if found else default


def value(node, name, default=None):
    k = kid(node, name)
    return k[1] if k and len(k) > 1 else default


def load(pcb):
    return parse(Path(pcb).read_text(encoding="utf-8-sig"))


def _angle(deg):
    """KiCad's GetOrientationDegrees(): normalised to (-180, 180]."""
    deg = float(deg) % 360.0
    return deg - 360.0 if deg > 180.0 else deg


# ---------------------------------------------------------------- components

_PAD_ATTR = {"thru_hole": 0, "smd": 1, "connect": 2, "np_thru_hole": 3}


def _mount_type(attrs):
    # As the original component-data script: all-"connect" pads report as "NPTH", no pads as "SMD".
    if all(a == 1 for a in attrs):
        return "SMD"
    if all(a == 0 for a in attrs):
        return "Through Hole"
    if all(a == 2 for a in attrs):
        return "NPTH"
    return "Mixed"


def components(board):
    rows, total = [], 0
    for fp in kids(board, "footprint"):
        at = kid(fp, "at")
        pads = kids(fp, "pad")
        total += len(pads)
        ref = next((p[2] for p in kids(fp, "property") if p[1] == "Reference"), "")
        rows.append([ref, f"{float(at[1]):.3f}", f"{float(at[2]):.3f}",
                     f"{_angle(at[3]) if len(at) > 3 else 0.0}",
                     "Front" if value(fp, "layer") == "F.Cu" else "Back",
                     _mount_type([_PAD_ATTR.get(p[2], 1) for p in pads]),
                     fp[1].split(":")[-1], len(pads),
                     "Yes" if "dnp" in (kid(fp, "attr") or []) else "No"])
    return rows, total


def write_components(board, out):
    rows, total = components(board)
    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["Reference", "X (mm)", "Y (mm)", "Rotation (°)", "Side", "Type",
                    "Footprint", "Pad Count", "DNP"])
        w.writerows(rows)
        w.writerow([])
        w.writerow(["Component Count", "Total Pads"])
        w.writerow([len(rows), total])


# ---------------------------------------------------------------- test points

def _net_class(net, project):
    """KiCad 10's effective net class, from the .kicad_pro: every explicit assignment and
    every matching pattern (wildcard or anchored regex), highest priority (lowest number)
    first, joined with "," as KiCad names a composite class. None -> "Default"."""
    ns = project.get("net_settings", {})
    found = list((ns.get("netclass_assignments") or {}).get(net) or [])
    for p in ns.get("netclass_patterns") or []:
        if _matches(p["pattern"], net) and p["netclass"] not in found:
            found.append(p["netclass"])
    if not found:
        return "Default"
    prio = {c.get("name"): c.get("priority", 0) for c in ns.get("classes") or []}
    return ",".join(sorted(dict.fromkeys(found), key=lambda c: prio.get(c, 0)))


def _matches(pattern, net):
    if fnmatchcase(net, pattern):
        return True
    try:
        return re.fullmatch(pattern, net) is not None
    except re.error:
        return False


def _pad_xy(fp_at, pad_at):
    """Pad centre on the board. Pad (at) is footprint-local; KiCad's y axis points down, so a
    footprint rotation of +a degrees maps (x, y) to (x cos a + y sin a, -x sin a + y cos a)."""
    a = math.radians(float(fp_at[3]) if len(fp_at) > 3 else 0.0)
    x, y = float(pad_at[1]), float(pad_at[2])
    return (float(fp_at[1]) + x * math.cos(a) + y * math.sin(a),
            float(fp_at[2]) - x * math.sin(a) + y * math.cos(a))


def testpoints(board, project):
    """Pads with Fabrication property "Test point pad", relative to the drill/aux origin with y
    up, as the kicad-testpoints plugin writes them with its default origin setting."""
    origin = kid(kid(board, "setup") or [], "aux_axis_origin") or ["", "0", "0"]
    ox, oy = float(origin[1]), float(origin[2])
    rows = []
    for fp in kids(board, "footprint"):
        back = value(fp, "layer") == "B.Cu"
        ref = next((p[2] for p in kids(fp, "property") if p[1] == "Reference"), "")
        for pad in kids(fp, "pad"):
            if value(pad, "property") != "pad_prop_testpoint":
                continue
            cx, cy = (round(v, 4) for v in _pad_xy(kid(fp, "at"), kid(pad, "at")))
            layers = (kid(pad, "layers") or [])[1:]
            net = kid(pad, "net")
            net = (net[-1] if net and len(net) > 1 else "")  # (net 3 "GND") or KiCad 10 (net "GND")
            rows.append({
                "source ref des": ref,
                "source pad": pad[1],
                "net": net,
                "net class": _net_class(net, project),
                "side": "BOTTOM" if back or "B.Cu" in layers else "TOP",
                "x": round(cx - ox, 4) + 0.0,  # + 0.0 turns -0.0 into 0.0
                "y": round(-(cy - oy), 4) + 0.0,
                "pad type": "THRU" if pad[2] in ("thru_hole", "np_thru_hole") else "SMT",
                "footprint side": "BOTTOM" if back else "TOP",
            })
    return rows


def write_testpoints(board, project, out):
    rows = testpoints(board, project)
    if not rows:
        raise SystemExit("No pads have Fabrication property 'Test point pad'.")
    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]), quoting=csv.QUOTE_NONNUMERIC)
        w.writeheader()
        w.writerows(rows)
    return len(rows)


# ---------------------------------------------------------------- stackup

def _mm(v):
    return int(float(v) * 100.0 + 0.5) / 100.0  # the original stackup script's rounding


def stackup(board, project):
    setup = kid(board, "setup") or []
    stack = kid(setup, "stackup") or []
    copper, dielectric, silk, mask = [], [], [], []
    for layer in kids(stack, "layer"):
        kind = value(layer, "type", "")
        if kind == "copper":
            copper.append(f"\t{layer[1]} : {value(layer, 'thickness')} mm")
        elif layer[1].startswith("dielectric"):
            # one dielectric can hold sublayers: (thickness ..) .. addsublayer (thickness ..) ..
            groups = [[]]
            for item in layer[2:]:
                if item == "addsublayer":
                    groups.append([])
                else:
                    groups[-1].append(item)
            for g in groups:
                g = ["sublayer", *g]
                dielectric.append("\t" + " : ".join([kind, value(g, "thickness", "?") + " mm",
                                                     value(g, "material", "?"),
                                                     value(g, "epsilon_r", "?"),
                                                     value(g, "loss_tangent", "?")]))
        elif "Silk Screen" in kind:
            silk.append(f"\t{kind} : {value(layer, 'color', 'white')}")
        elif "Solder Mask" in kind:
            mask.append(f"\t{kind} : {value(layer, 'color', 'green')} : "
                        f"{value(layer, 'thickness', '0.01')}")
    names = project.get("text_variables", {})  # ${NAME} from Board Setup > Text Variables
    var = lambda t: re.sub(r"\$\{([^}]+)\}", lambda m: names.get(m[1], m[0]), t)  # noqa: E731
    title = [f"\t{k[0]} : {var(k[-1])}" if k[0] != "comment" else f"\tcomment {k[1]} : {var(k[2])}"
             for k in (kid(board, "title_block") or [])[1:] if isinstance(k, list) and len(k) > 1]
    rules = project.get("board", {}).get("design_settings", {}).get("rules", {})
    rule = lambda name: f"{_mm(rules.get(name, 0)):3.2f}mm"  # noqa: E731
    thickness = float(value(kid(board, "general") or [], "thickness", 0))
    return "\n".join([
        "Title", *title,
        f"\n\nBoard Thickness = {thickness:2.1f} mm",
        f"\n\nCopper Layer  = {len(copper)} ",
        "\n\nCopper Layers ", *copper,
        "\n\nDiElectric Layers ", *dielectric,
        f"\n\nImpedance Control : {value(stack, 'dielectric_constraints') == 'yes'}",
        "\nSilkScreen Layers ", *silk,
        "\n\nSolderMask Layers ", *mask,
        "\n\nDRC Rules ",
        "Track Rules ",
        f"\tMin Track Width : {rule('min_track_width')}",
        f"\tMin Track Spacing : {rule('min_clearance')}",
        f"\tMin Cu to Edge : {rule('min_copper_edge_clearance')}",
        "Via Rules ",
        f"\tMin Via hole  : {rule('min_through_hole_diameter')}",
        f"\tMin Via Dia : {rule('min_via_diameter')}",
        f"\tMin Annular width : {rule('min_via_annular_width')}",
        f"\tMin hole to hole spacing : {rule('min_hole_to_hole')}",
    ]) + "\n"


def main(argv):
    kind, pcb, out = argv
    board = load(pcb)
    pro = Path(pcb).with_suffix(".kicad_pro")
    if not pro.is_file():  # net classes and DRC rules live there; never guess them
        raise SystemExit(f"{pro.name} not found beside the board.")
    project = json.loads(pro.read_text(encoding="utf-8-sig"))
    if kind == "components":
        write_components(board, out)
    elif kind == "testpoints":
        print(f"{write_testpoints(board, project, out)} test points -> {out}")
    elif kind == "stackup":
        with open(out, "w", encoding="utf-8") as f:  # text mode: CRLF on Windows, as before
            f.write(stackup(board, project))
    else:
        raise SystemExit(f"unknown export {kind!r}")


if __name__ == "__main__":
    main(sys.argv[1:])
