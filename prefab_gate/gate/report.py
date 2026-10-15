"""Human and machine renderings of a verdict."""
import json
from dataclasses import asdict


def _line(finding):
    where = f"  [{', '.join(finding.items)}]" if finding.items else ""
    return (f"  {finding.type or finding.kind:32} {finding.description}{where}\n"
            f"      {finding.reason}")


def _switched_off(verdict) -> str:
    """What the gate was told not to look at.

    A receipt whose job is "what did the gate knowingly allow past" has to
    record what was switched off, or PASSED overstates its own coverage.
    """
    parts = []
    if verdict.excluded:
        parts.append(f"{verdict.excluded} excluded")
    if verdict.ignored_checks:
        keys = ", ".join(c.get("key") or c.get("description", "?")
                         for c in verdict.ignored_checks)
        parts.append(f"{len(verdict.ignored_checks)} check categories disabled "
                     f"in project settings ({keys})")
    return f"Not checked: {'; '.join(parts)}." if parts else ""


def render_text(verdict) -> str:
    out = []
    if verdict.passed:
        out.append(f"PASSED — nothing blocking. {len(verdict.cosmetic)} cosmetic "
                   f"finding(s) waved through.")
    else:
        # "No package was produced", not "no files": DRC runs with
        # --refill-zones --save-board, so the board file itself may already
        # have been rewritten — and a line above may have just said so.
        out.append(f"BLOCKED — {len(verdict.blocking)} blocking finding(s). "
                   "No package was produced.")
        out.append("")
        out.append("Blocking:")
        out.extend(_line(f) for f in verdict.blocking)
    if verdict.cosmetic:
        out.append("")
        out.append(f"Cosmetic ({len(verdict.cosmetic)}):")
        out.extend(_line(f) for f in verdict.cosmetic)
    switched_off = _switched_off(verdict)
    if switched_off:
        out.append("")
        out.append(switched_off)
    return "\n".join(out)


def render_json(verdict) -> str:
    return json.dumps(verdict_json(verdict), indent=2)


def verdict_json(verdict) -> dict:
    """The verdict as plain data — shared by --json and the package manifest."""
    return {
        "passed": verdict.passed,
        "blocking": [asdict(f) for f in verdict.blocking],
        "cosmetic": [asdict(f) for f in verdict.cosmetic],
        "excluded": verdict.excluded,
        "ignored_checks": list(verdict.ignored_checks),
        # The receipt has to distinguish "parity clean" from "parity never ran".
        "parity": asdict(verdict.parity),
    }
