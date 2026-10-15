"""Trace ampacity checks: the two things DRC structurally cannot see.

DRC enforces the board-wide ``min_track_width``. It does not enforce a
netclass's ``track_width``, which KiCad treats as a default for newly routed
tracks rather than a constraint. So a net assigned to a 0.8 mm power netclass
can be routed at 0.2 mm and DRC will pass with zero errors.

Two checks live here:

``netclass_conformance``
    A net routed narrower than the netclass it was assigned to. Pure geometry,
    no physics, no part knowledge. This is the cheap one and it catches the
    common case.

``fuse_ampacity``
    The invariant that actually matters on a power input: **the current that
    damages the narrowest trace on a fused net must exceed the current at which
    the fuse is guaranteed to trip.** A fuse protecting a trace that fails
    before it does is not protecting anything.

Both were written after a shipped board reached its polyfuse through 9.06 mm of
0.2 mm trace. The net had been left out of the power netclass's name patterns,
so it silently inherited the 0.2 mm default; DRC, ERC and net-assignment
checking all passed. See the linktovideo VBUS trace advisory.
"""
import json
import math
import re

from gate.model import Finding

# IPC-2221 external-layer constant. Internal layers use 0.024; external is the
# right choice for a gate because it is what a top-layer power trace sees.
IPC_K = 0.048
OZ_UM = 35e-3               # mm, 1 oz finished copper
FR4_TG_RISE = 105.0         # K rise over 25 C ambient reaching a 130 C Tg

# Thermal healing length for copper on FR4, in mm. A narrow neck much shorter
# than this is held down by conduction into the wider copper on either side and
# never reaches the temperature an equally narrow *long* run would.
#
# Calibrated against a distributed electrothermal model (ngspice) of a 0.2 mm
# neck flanked by 0.8 mm copper, whose damage current falls 5.10 A -> 2.67 A as
# the neck grows 0.5 mm -> 9.06 mm. A single-lambda fin fit spans 2.5-3.6 mm
# across that range; the low end is taken because it under-credits the healing
# and so errs toward reporting. Without this term the check cannot tell a
# hairline 9 mm run from the 0.7 mm neck-down at a pad that every board has,
# and it flags both -- which is how a gate gets ignored.
HEAL_MM = 2.5


def ipc_current(width_mm, rise_k, thickness_mm=OZ_UM):
    """IPC-2221 external-layer allowable current (A) at a given rise."""
    area_sqmil = (width_mm / 0.0254) * (thickness_mm / 0.0254)
    return IPC_K * rise_k ** 0.44 * area_sqmil ** 0.725


def heal_factor(run_mm, heal_mm=HEAL_MM):
    """Fraction of the infinite-trace temperature rise a run of this length reaches.

    Fin solution for a uniformly heated strip whose ends are held down by the
    wider copper they join: 1 - sech(L / 2*lambda). Approaches 1 for a long run,
    and falls toward 0 for a neck much shorter than the healing length.
    """
    if run_mm is None:
        return 1.0
    return 1.0 - 1.0 / math.cosh(run_mm / (2.0 * heal_mm))


def damage_current(width_mm, thickness_mm=OZ_UM, rise_k=FR4_TG_RISE,
                   run_mm=None, heal_mm=HEAL_MM):
    """Current at which a trace reaches FR4's glass transition.

    Steady state, still air, no credit for an adjacent plane -- deliberately the
    conservative reading. With ``run_mm`` given, the rise is scaled by the
    healing factor for a run of that length, so a short neck-down at a pad is
    not treated as if it were a long hairline.
    """
    phi = heal_factor(run_mm, heal_mm)
    if phi <= 0:
        return float("inf")
    return ipc_current(width_mm, rise_k / phi, thickness_mm)


def _segments(board_text):
    """(net, width_mm) for every routed segment. Vias and zones are not tracks."""
    return [(m.group(7), float(m.group(5))) for m in re.finditer(
        r'\(segment\s*\(start ([-\d.]+) ([-\d.]+)\)\s*\(end ([-\d.]+) ([-\d.]+)\)'
        r'\s*\(width ([\d.]+)\)\s*\(layer "([^"]+)"\)\s*(?:\(locked yes\)\s*)?'
        r'\(net "([^"]*)"\)', board_text)]


def _narrowest(board_text):
    """{net: narrowest routed width in mm}."""
    out = {}
    for net, w in _segments(board_text):
        if net and (net not in out or w < out[net]):
            out[net] = w
    return out


def _runs(board_text):
    """{net: [(width_mm, contiguous_length_mm), ...]} by walking connectivity.

    Segments of equal width that touch end-to-end are one run. Length matters
    as much as width: a long hairline is a bottleneck, a short neck-down at a
    pad is not.
    """
    from collections import defaultdict
    per_net = defaultdict(list)
    for m in re.finditer(
            r'\(segment\s*\(start ([-\d.]+) ([-\d.]+)\)\s*\(end ([-\d.]+) ([-\d.]+)\)'
            r'\s*\(width ([\d.]+)\)\s*\(layer "([^"]+)"\)\s*(?:\(locked yes\)\s*)?'
            r'\(net "([^"]*)"\)', board_text):
        x1, y1, x2, y2, w, layer, net = m.groups()
        if not net:
            continue
        per_net[net].append(((round(float(x1), 3), round(float(y1), 3), layer),
                             (round(float(x2), 3), round(float(y2), 3), layer),
                             float(w)))
    out = {}
    for net, segs in per_net.items():
        adj = defaultdict(list)
        for i, (a, b, w) in enumerate(segs):
            adj[(a, w)].append(i)
            adj[(b, w)].append(i)
        seen, runs = set(), []
        for i, (a, b, w) in enumerate(segs):
            if i in seen:
                continue
            stack, total = [i], 0.0
            seen.add(i)
            while stack:
                j = stack.pop()
                aa, bb, ww = segs[j]
                total += math.dist(aa[:2], bb[:2])
                for end in (aa, bb):
                    for k in adj[(end, ww)]:
                        if k not in seen:
                            seen.add(k)
                            stack.append(k)
            runs.append((w, total))
        out[net] = runs
    return out


def _limiting_run(board_text, net, thickness_mm=OZ_UM):
    """(width, length, damage_current) of the run that most limits this net.

    Healing is credited only to runs *narrower* than the net's dominant width.
    The widest run is what sets the net's base temperature -- there is nothing
    wider on either side of it to conduct its heat away -- so it is evaluated
    unhealed. Crediting every run, including the widest, would over-report the
    net's capacity.
    """
    runs = _runs(board_text).get(net, [])
    if not runs:
        return None
    base = max(w for w, _ in runs)
    best = None
    for w, ln in runs:
        cur = damage_current(w, thickness_mm,
                             run_mm=None if w >= base else ln)
        if best is None or cur < best[2]:
            best = (w, ln, cur)
    return best


def _pattern_matches(pattern, net):
    """KiCad netclass patterns are globs, not regexes."""
    rx = "^" + ".*".join(re.escape(p) for p in pattern.split("*")) + "$"
    return re.match(rx, net) is not None


def _netclass_of(net, project):
    """(class_name, track_width) for a net, or (None, None)."""
    ns = project.get("net_settings", {}) or {}
    widths = {c.get("name"): c.get("track_width")
              for c in (ns.get("classes") or [])}
    for entry in (ns.get("netclass_patterns") or []):
        if _pattern_matches(entry.get("pattern", ""), net):
            name = entry.get("netclass")
            return name, widths.get(name)
    assignments = ns.get("netclass_assignments") or {}
    if net in assignments:
        name = assignments[net]
        return name, widths.get(name)
    return None, None


def netclass_conformance(board_text, project, tolerance=1e-6):
    """Findings for nets routed narrower than their assigned netclass width."""
    findings = []
    for net, narrowest in sorted(_narrowest(board_text).items()):
        name, want = _netclass_of(net, project)
        if not name or not want:
            continue
        if narrowest < float(want) - tolerance:
            findings.append(Finding(
                kind="ampacity", type="netclass_width",
                description=(
                    f"Net {net} is in netclass {name}, which specifies "
                    f"{want} mm track width, but is routed as narrow as "
                    f"{narrowest} mm."),
                severity="warning",
                items=(f"Net {net}", f"netclass {name}"),
                blocking=False,
                reason=("DRC enforces the board min_track_width, not a "
                        "netclass track_width, so this passes DRC. Reported "
                        "rather than blocking: a short neck-down at a pad is "
                        "normal and legitimate")))
    return findings


def _fuse_trip_currents(board_text):
    """{reference: trip_current_A} for footprints that look like fuses.

    Reads the trip current from a ``Spec`` property when one is present -- the
    only place a board file records it. A fuse whose trip current cannot be
    read is reported separately rather than guessed at.
    """
    fuses = {}
    for fp in re.finditer(
            r'\(footprint(.*?)\n\t\)\n(?=\t\(footprint|\t\(gr_|\t\(segment|\t\(zone|\t\(via)',
            board_text, re.S):
        blk = fp.group(1)
        ref = re.search(r'\(property "Reference" "(F\d+)"', blk)
        if not ref:
            continue
        trip = None
        spec = re.search(r'\(property "Spec" "([^"]*)"', blk)
        if spec:
            m = re.search(r'I[_ ]?T\s*[:=]?\s*([\d.]+)\s*A', spec.group(1), re.I)
            if m:
                trip = float(m.group(1))
        fuses[ref.group(1)] = trip
    return fuses


def _nets_of(board_text, reference):
    """Nets touched by a footprint's pads."""
    nets = set()
    for fp in re.finditer(
            r'\(footprint(.*?)\n\t\)\n(?=\t\(footprint|\t\(gr_|\t\(segment|\t\(zone|\t\(via)',
            board_text, re.S):
        blk = fp.group(1)
        ref = re.search(r'\(property "Reference" "([^"]+)"', blk)
        if not ref or ref.group(1) != reference:
            continue
        nets.update(m.group(1) for m in re.finditer(r'\(net "([^"]*)"\)', blk))
    return {n for n in nets if n and not n.startswith("unconnected-")}


def fuse_ampacity(board_text, thickness_mm=OZ_UM):
    """Findings where a fused net's narrowest trace fails below the fuse trip."""
    findings = []
    for ref, trip in sorted(_fuse_trip_currents(board_text).items()):
        nets = _nets_of(board_text, ref)
        if trip is None:
            if nets:
                findings.append(Finding(
                    kind="ampacity", type="fuse_trip_unknown",
                    description=(
                        f"{ref} looks like a fuse but records no trip current, "
                        f"so its nets ({', '.join(sorted(nets))}) cannot be "
                        f"checked against it. Add a Spec property containing "
                        f"e.g. 'IT 3.8 A'."),
                    severity="warning",
                    items=(f"Fuse {ref}",), blocking=False,
                    reason="cannot verify an invariant without the trip current"))
            continue
        for net in sorted(nets):
            limiting = _limiting_run(board_text, net, thickness_mm)
            if limiting is None:
                continue
            width, run, damage = limiting
            if damage <= trip:
                findings.append(Finding(
                    kind="ampacity", type="fuse_trace_ordering",
                    description=(
                        f"Net {net} is protected by {ref} (trips at {trip} A) "
                        f"but has a {run:.2f} mm contiguous run at {width} mm "
                        f"width, which reaches FR4's glass transition at about "
                        f"{damage:.2f} A. The trace is damaged before the fuse "
                        f"is guaranteed to act, so the fuse does not protect "
                        f"it. Widen the run, or verify it carries no current."),
                    severity="error",
                    items=(f"Net {net}", f"Fuse {ref}"), blocking=True,
                    reason=("trace damage current must exceed the fuse trip "
                            "current")))
    return findings


def check(board_path, project_path=None, thickness_mm=OZ_UM):
    """Run both ampacity checks over a board. Returns a list of Findings."""
    board_text = open(board_path, encoding="utf-8").read()
    findings = list(fuse_ampacity(board_text, thickness_mm))
    if project_path:
        try:
            project = json.load(open(project_path, encoding="utf-8"))
        except (OSError, ValueError):
            project = None
        if project:
            findings = netclass_conformance(board_text, project) + findings
    return findings
