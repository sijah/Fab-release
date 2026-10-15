#!/usr/bin/env python3
"""Pre-fab gate: no fab package without a passing check."""
import argparse
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from gate.ampacity import check as check_ampacity     # noqa: E402
from gate.classify import (ReportInvalid, classify,      # noqa: E402
                           validate_report)
from gate.export import (export_package, locate_schematic,  # noqa: E402
                         schematic_candidates, schematic_for)
from gate.kicad import (BoardUnreadable, KicadUnavailable,  # noqa: E402
                        locate_cli, probe_capability, run_drc)
from gate.manifest import (VerifiedArtifactChanged,      # noqa: E402
                           build_manifest, require_unchanged, sha256)
from gate.model import Finding, Parity, Verdict          # noqa: E402
from gate.report import render_json, render_text        # noqa: E402


def _cli_version(cli):
    result = subprocess.run([cli, "version"], capture_output=True, text=True)
    return (result.stdout or "").strip()


DEFAULT_DEPS = {"locate_cli": locate_cli, "probe_capability": probe_capability,
                "run_drc": run_drc, "export_package": export_package,
                "cli_version": _cli_version, "file_hash": sha256,
                "locate_schematic": locate_schematic,
                "schematic_candidates": schematic_candidates}


class GateArgumentParser(argparse.ArgumentParser):
    """argparse exits 2 on a usage error; here 2 means "blocked by findings".

    A CI job cannot tell a typo'd flag from a board that failed verification if
    both exit 2. Usage errors are an environment problem — the gate could not
    run — so they take exit 3 along with everything else in that class. The
    spec fixes the contract at 0/2/3; a fourth code is not ours to invent.
    """

    def error(self, message):
        self.print_usage(sys.stderr)
        print(f"{self.prog}: error: {message}", file=sys.stderr)
        raise SystemExit(3)


def _build_parser():
    parser = GateArgumentParser(prog="prefab_gate", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True,
                                parser_class=GateArgumentParser)
    for name in ("check", "package"):
        p = sub.add_parser(name)
        p.add_argument("board")
        if name == "package":
            # `check` produces no package, so an --out it silently ignored was
            # a promise it could not keep.
            p.add_argument("--out", default="fab")
        p.add_argument("--json", action="store_true")
        p.add_argument("--strict", action="store_true")
        p.add_argument("--no-ampacity", action="store_true",
                       help="skip the trace ampacity checks")
        p.add_argument("--schematic", default=None, metavar="PATH",
                       help="schematic the BOM is exported from, when it is not "
                            "beside the board under the board's own name. It "
                            "cannot redirect parity: kicad-cli derives that "
                            "from the board's basename.")
        p.add_argument("--no-parity", action="store_true",
                       help="skip schematic parity (for a PCB-only design). "
                            "Recorded as a cosmetic finding, never silently.")
    return parser


def _resolve_parity(args, d):
    """Decide whether parity can run, and record why when it cannot.

    Returns (parity, schematic). The schematic is resolved exactly once here
    and carried onwards, because it is two different things at once: the file
    parity is checked against, and the file the BOM is exported from. Looking
    it up again at export time let those diverge — parity against one
    schematic, a BOM from whatever a second lookup happened to find.

    They are still not the same field. --no-parity has a BOM source and no
    parity, and a schematic under an unconventional name is a BOM source that
    kicad-cli will not read for parity, so the path outlives parity.ran.

    Never a silent skip: every branch that does not run parity carries a reason
    that ends up in the report and the manifest.
    """
    if args.no_parity:
        return Parity(ran=False, waived=True, reason=(
            "skipped at your request (--no-parity); the board was not compared "
            "against any schematic")), d["locate_schematic"](args.board,
                                                             args.schematic)
    conventional_path = schematic_for(args.board)
    if args.schematic and os.path.exists(conventional_path) and (
            os.path.abspath(args.schematic) != os.path.abspath(conventional_path)):
        # kicad-cli checks the sibling regardless, so the package would pair a
        # BOM exported from the override with a parity result from a different
        # file. Refusing beats shipping two halves of two designs.
        raise KicadUnavailable(
            f"--schematic points at {args.schematic}, but {conventional_path} "
            "exists beside the board and kicad-cli derives the schematic from "
            "the board's basename, so parity would check that one while the BOM "
            "came from yours. Rename the schematic you mean to "
            f"{os.path.basename(conventional_path)}, or move the sibling aside.")

    schematic = d["locate_schematic"](args.board, args.schematic)
    conventional = os.path.basename(conventional_path)
    if schematic is not None and os.path.abspath(schematic) == os.path.abspath(
            conventional_path):
        return Parity(ran=True, schematic=schematic), schematic

    if schematic is not None:
        # locate_schematic's fallback found one, but it is not the file
        # kicad-cli will read. Reporting ran=True here put a name in the
        # receipt that nothing had checked: kicad-cli looks for the sibling,
        # fails to find it, and exits 0 with an empty parity list anyway.
        where = (f"the only schematic beside the board is "
                 f"{os.path.basename(schematic)}, which kicad-cli will not "
                 f"read for parity: it looks for {conventional}")
    else:
        candidates = d["schematic_candidates"](args.board)
        if not candidates:
            where = f"no .kicad_sch beside the board (looked for {conventional})"
        else:
            names = ", ".join(os.path.basename(c) for c in candidates)
            where = (f"{len(candidates)} schematics beside the board and none "
                     f"named {conventional}, so the gate will not guess which "
                     f"one is the design: {names}")
    return Parity(ran=False, reason=(
        f"parity could not run: {where}. kicad-cli derives the schematic from "
        "the board's basename and cannot be pointed elsewhere, so parity needs "
        f"a schematic named {conventional} sitting beside the board. Rename it, "
        "or accept a PCB-only check with --no-parity. (--schematic chooses the "
        "schematic the BOM is exported from; it cannot redirect parity.)")), \
        schematic


def _emit(args, verdict) -> None:
    """stdout carries the report — JSON when asked for, text otherwise.

    Never both. --json used to append the JSON *after* the full text report,
    so anything piping stdout into a parser got up to 252 KB of prose first.
    """
    print(render_json(verdict) if args.json else render_text(verdict))


def _note(args, message: str) -> None:
    """Chatter that is not the report. Under --json it must leave stdout alone."""
    print(message, file=sys.stderr if args.json else sys.stdout)


def _unreadable(exc, parity) -> Verdict:
    """A board kicad-cli cannot load is a blocking finding, not an exit-3.

    It travels as a normal verdict so the report, --json and the exit code all
    describe it the same way everything else is described.
    """
    verdict = Verdict(parity=Parity(
        ran=False, schematic=parity.schematic,
        reason="the board could not be loaded, so parity never ran"))
    verdict.blocking.append(Finding(
        kind="board", type="board_unreadable", description=str(exc),
        severity="error", items=(), blocking=True,
        reason="kicad-cli ran and could not load this board"))
    return verdict


def _project_for(board):
    """Sibling .kicad_pro, when there is one. Netclass widths live there."""
    candidate = os.path.splitext(board)[0] + ".kicad_pro"
    return candidate if os.path.exists(candidate) else None


def _gate(args, d) -> int:
    cli = d["locate_cli"]()
    d["probe_capability"](cli)
    parity, schematic = _resolve_parity(args, d)
    # --refill-zones --save-board can rewrite the board, so this is only good
    # for telling the user their git diff is expected. The hash that matters
    # is taken after the DRC, below.
    before = d["file_hash"](args.board)
    try:
        drc, parity_error = d["run_drc"](cli, args.board, parity=parity.ran)
    except BoardUnreadable as exc:
        _emit(args, _unreadable(exc, parity))
        return 2
    # Before anything is judged: every section the verdict is built from has to
    # be there. parity.ran is still the *requested* value at this point, which
    # is what decides whether a parity section was asked for.
    validate_report(drc, parity_requested=parity.ran)
    if parity.ran and parity_error:
        # kicad-cli exits 0 and emits an empty parity list when it could not
        # load the schematic. The violations in the same report are still
        # valid; it is the parity half that did not run.
        parity = Parity(ran=False, schematic=parity.schematic, reason=(
            "kicad-cli could not run the parity tests against "
            f"{parity.schematic}, and exited 0 reporting no parity issues "
            "anyway — its exit code cannot be trusted here. It said: "
            f"{parity_error.strip()}"))
    # The state the verdict below actually describes: after the refill, which
    # is the last thing entitled to touch these files. Everything downstream
    # has to still be this, or the gate is vouching for something it did not
    # check.
    verified_board = d["file_hash"](args.board)
    verified_schematic = (None if schematic is None
                          else d["file_hash"](schematic))
    if verified_board != before:
        _note(args, f"Note: refilling updated the zone fills in {args.board} — "
                    "the board file has been modified and should be committed.\n")

    verdict = classify(drc, strict=args.strict, parity=parity)
    # Trace ampacity is checked from the board file directly, because it is
    # precisely what kicad-cli cannot see: DRC enforces the board-wide
    # min_track_width, never a netclass track_width, so a power net routed at a
    # quarter of its class width passes DRC with zero errors.
    if not args.no_ampacity:
        for finding in check_ampacity(args.board, _project_for(args.board)):
            (verdict.blocking if finding.blocking else verdict.cosmetic).append(finding)
    _emit(args, verdict)

    code = 0 if verdict.passed else 2
    if verdict.passed and args.command == "package":
        def require_verified_inputs():
            require_unchanged(args.board, verified_board, "the board",
                              hasher=d["file_hash"])
            if schematic is not None:
                require_unchanged(schematic, verified_schematic,
                                  "the schematic", hasher=d["file_hash"])

        def write_manifest(staging):
            # Written into the staging directory, so the one os.replace inside
            # export_package publishes the package and its receipt together.
            # Writing it afterwards left a window in which a complete-looking
            # fab package existed with no record of what the gate let past.
            #
            # The last gate before publication, so the inputs are checked again
            # here: this runs after every export has finished, which closes the
            # window an export that rewrites its own source would otherwise
            # open.
            require_verified_inputs()
            files = [os.path.join(root, f)
                     for root, _, fs in os.walk(staging) for f in fs]
            manifest = build_manifest(args.board, d["cli_version"](cli), verdict,
                                      files, staging, strict=args.strict,
                                      out_dir=args.out,
                                      checked_board_sha256=verified_board,
                                      schematic=schematic,
                                      checked_schematic_sha256=verified_schematic)
            with open(os.path.join(staging, "manifest.json"), "w") as fh:
                json.dump(manifest, fh, indent=2)

        # Checked once here so a board that already moved fails before three
        # exports run against it, and again in write_manifest so nothing is
        # published on inputs that moved during them.
        require_verified_inputs()
        # The BOM comes from the schematic parity was resolved against, not a
        # second lookup. None means PCB-only: no BOM, rather than a failed
        # export after the board files are already written.
        package = d["export_package"](cli, args.board, args.out,
                                      on_staged=write_manifest,
                                      schematic=schematic)
        _note(args, f"\nPackage written to {package}")

    return code


def main(argv=None, deps=None) -> int:
    d = dict(DEFAULT_DEPS)
    d.update(deps or {})

    args = _build_parser().parse_args(argv)

    if not os.path.isfile(args.board):
        print(f"board not found: {args.board}\n"
              "Pass the path to a .kicad_pcb file. The gate cannot verify a board "
              "it cannot read.", file=sys.stderr)
        return 3

    # Every message below goes to stderr. _emit has usually already put the
    # report on stdout by the time these fire, so printing there would append
    # prose to a --json document and break the consumer parsing it. It also
    # puts runtime errors on the same stream GateArgumentParser.error already
    # uses for usage errors.
    try:
        return _gate(args, d)
    except KicadUnavailable as exc:
        print(str(exc), file=sys.stderr)
        return 3
    except VerifiedArtifactChanged as exc:
        # The board may well have been fine. This is the gate losing its
        # ability to say so, which is the gate-error case, not exit 2.
        print(str(exc), file=sys.stderr)
        return 3
    except ReportInvalid as exc:
        # The gate could not check, which is not the same as checking and
        # failing. Exit 3 keeps that apart from a blocked board's exit 2.
        print(f"{exc}", file=sys.stderr)
        return 3
    except json.JSONDecodeError as exc:
        # kicad-cli wrote something that is not the DRC JSON the gate parses.
        print(f"kicad-cli's DRC output could not be parsed as JSON: {exc}\n"
              "The gate cannot judge a board it cannot read a report for.",
              file=sys.stderr)
        return 3
    except OSError as exc:
        # Unreadable board, unwritable output directory, a manifest that could
        # not be written. All "the gate could not run", never a silent pass.
        print(f"the gate could not complete: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
