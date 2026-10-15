"""Turn parsed kicad-cli DRC JSON into a Verdict.

Pure: no filesystem, no subprocess. That is what makes the policy testable
without KiCad installed.
"""
from dataclasses import replace

from gate.model import Finding, Parity, Verdict

# Parity descriptions are matched by substring because kicad-cli's `type` field
# is too coarse: footprint_symbol_mismatch covers both a genuine footprint
# mismatch and a trivial exclude-from-BOM difference.
BLOCKING_PARITY = (
    "doesn't match footprint given by symbol",
    "'Do not populate' settings differ",
    "Missing footprint",
    "doesn't match net given by schematic",
    "Pad missing net given by schematic",
    "No corresponding pin found in schematic",
    "No pad found for pin",
    # Harvested from a 303-board corpus sweep, 2026-08-30. The board carries a
    # part the schematic does not — the mirror of "Missing footprint" above.
    # Not decoration: of 1295 items the sweep saw, the designators were 234 D,
    # 118 S, 83 MX, 77 C, 50 R, 17 U. Those get fabricated and assembled while
    # absent from the schematic-derived BOM.
    "Extra footprint",
    # Same sweep. Two footprints share a reference designator, so neither the
    # fab BOM nor the placement file can tell them apart.
    "Duplicate footprints",
)
COSMETIC_PARITY = (
    "Missing symbol field",
    "'Exclude from bill of materials' settings differ",
    "doesn't match symbol value",
    "differs (PCB:",
    # Same sweep. A footprint filter is the symbol author's list of footprints
    # they anticipated, not a fact about this board; picking a compatible
    # footprint outside that list is ordinary practice. Distinct from
    # "doesn't match footprint given by symbol" in BLOCKING_PARITY, which is
    # the symbol's actual assigned footprint disagreeing with the board.
    "doesn't match symbol's footprint filters",
)


class ReportInvalid(Exception):
    """kicad-cli's report is missing a section the gate has to judge.

    Not a blocking finding: a blocked board is one the gate checked and
    rejected, whereas this is the gate being unable to check at all. The two
    have to stay distinguishable, so this takes the gate-error exit.
    """


# Sections whose presence is the evidence that the check completed.
REQUIRED_SECTIONS = ("violations", "unconnected_items")


def validate_report(drc, *, parity_requested: bool) -> None:
    """Require every section the verdict is built from, before building it.

    classify used to read its sections with drc.get(key, []), which made an
    absent section indistinguishable from one that found nothing — so a
    truncated report classified as a clean board and, under `package`,
    published a fab package. Nothing about "no result was supplied" is
    evidence of "zero findings".

    schematic_parity is required only when parity was actually requested.
    KiCad 10.0.6 emits it either way, so this rejects nothing valid; it just
    declines to demand a section the gate did not ask for.
    """
    if not isinstance(drc, dict):
        raise ReportInvalid(
            f"kicad-cli's DRC report is {type(drc).__name__}, not an object. "
            "The gate cannot judge a board it has no report for.")
    required = REQUIRED_SECTIONS + (
        ("schematic_parity",) if parity_requested else ())
    for key in required:
        if key not in drc:
            raise ReportInvalid(
                f"kicad-cli's DRC report has no {key!r} section, so there is no "
                "evidence that check ran. An absent section is not an empty "
                "one. Re-run the DRC; if it keeps happening, the kicad-cli "
                "writing this report is not one the gate can vouch for.")
        if not isinstance(drc[key], list):
            raise ReportInvalid(
                f"kicad-cli's DRC report has {key!r} as "
                f"{type(drc[key]).__name__}, not a list, so the gate cannot "
                "tell what that check found.")


def _items(entry):
    return tuple(i.get("description", "") for i in entry.get("items", []))


def _judge_severity(severity: str):
    """An unrecognised severity blocks.

    Treating anything-but-"error" as cosmetic made an absent or future severity
    purely decorative, while the parity path deliberately fails closed on an
    unfamiliar message. The two policies now agree: what the gate does not
    understand, it does not wave through.
    """
    if severity == "error":
        return True, "DRC severity is error"
    if severity == "warning":
        return False, "DRC severity is warning"
    return True, ("unrecognised DRC severity "
                  f"{severity!r}, blocking by default")


def parity_not_run(parity: Parity) -> Finding:
    """Parity that could not run is a finding, never a silent skip.

    Blocking by default, exactly like an unrecognised parity description: the
    gate cannot tell an unchecked board from a clean one. Cosmetic only when
    the user waived it with --no-parity — a PCB-only design is legitimate, but
    it may not pass quietly.
    """
    return Finding(
        kind="parity", type="parity_not_run",
        description="Schematic parity did not run",
        severity="warning" if parity.waived else "error",
        items=(), blocking=not parity.waived, reason=parity.reason)


def classify(drc: dict, strict: bool = False, parity: Parity = None) -> Verdict:
    parity = Parity(ran=True) if parity is None else parity
    # Recorded, not judged: rules the project file sets to "ignore" never
    # appear as findings at all, so a clean verdict on a board with five
    # checks switched off would otherwise say nothing about them.
    verdict = Verdict(ignored_checks=list(drc.get("ignored_checks", []) or []),
                      parity=parity)

    def place(finding):
        if finding.blocking:
            verdict.blocking.append(finding)
        elif strict:
            verdict.blocking.append(replace(
                finding, blocking=True,
                reason=finding.reason + " (promoted by --strict)"))
        else:
            verdict.cosmetic.append(finding)

    for v in drc.get("violations", []):
        severity = v.get("severity", "")
        # KiCad <=9 reports severity "exclusion"; KiCad 10 keeps the rule's
        # severity and adds "excluded": true.
        if severity == "exclusion" or v.get("excluded") is True:
            # Excluded in the GUI: counted, not judged. The count is the point —
            # it is the difference between "nothing was wrong" and "you told me
            # not to look".
            verdict.excluded += 1
            continue
        blocking, reason = _judge_severity(severity)
        place(Finding(kind="violation", type=v.get("type", ""),
                      description=v.get("description", ""), severity=severity,
                      items=_items(v), blocking=blocking, reason=reason))

    for u in drc.get("unconnected_items", []):
        place(Finding(kind="unconnected", type=u.get("type", "unconnected"),
                      description=u.get("description", ""), severity="error",
                      items=_items(u), blocking=True,
                      reason="unconnected items always block"))

    for p in drc.get("schematic_parity", []):
        description = p.get("description", "")
        if any(s in description for s in BLOCKING_PARITY):
            blocking, reason = True, "structural parity mismatch"
        elif any(s in description for s in COSMETIC_PARITY):
            blocking, reason = False, "metadata-only parity mismatch"
        else:
            blocking = True
            reason = ("unrecognised parity description, blocking by default: "
                      f"{description!r}")
        place(Finding(kind="parity", type=p.get("type", ""), description=description,
                      severity=p.get("severity", "warning"), items=_items(p),
                      blocking=blocking, reason=reason))

    if not parity.ran:
        place(parity_not_run(parity))

    return verdict
