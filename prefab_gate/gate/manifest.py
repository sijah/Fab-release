"""The package receipt.

Answers, months later: what was sent, from which board state, under which
policy, and what did the gate knowingly allow past?
"""
import hashlib
import os
from datetime import datetime, timezone

from gate.report import verdict_json


def sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


class VerifiedArtifactChanged(Exception):
    """An input moved between being verified and being packaged.

    Not a blocking finding — the board may well have been fine. It is the
    gate losing its ability to say so, which is the gate-error case.
    """


def require_unchanged(path: str, expected: str, what: str, hasher=sha256) -> None:
    """Refuse to proceed unless `path` still hashes to what it did when checked.

    The before/after hashes around the DRC only ever documented that KiCad's
    own refill rewrote the board. Nothing required the file exported to
    fabrication to be the file the DRC passed, so an editor saving in the
    window between them produced a package that looked reproducible — an
    accurate hash of an unverified board — while quietly breaking the one
    guarantee the gate exists to make.
    """
    actual = hasher(path)
    if actual != expected:
        raise VerifiedArtifactChanged(
            f"{what} changed after it was checked: {path} hashed {expected} "
            f"when the gate verified it and {actual} now. The package was not "
            "published, because the gate can only vouch for what it checked. "
            "Re-run the gate on the current file.")


def build_manifest(board: str, cli_version: str, verdict, files, package_root: str,
                   *, strict: bool, out_dir: str, checked_board_sha256: str,
                   schematic: str = None,
                   checked_schematic_sha256: str = None) -> dict:
    """Build the receipt. Every argument is required on purpose.

    package_root is not optional: without it file names fall back to bare
    basenames, so two gerbers from different subdirectories collide and the
    receipt stops identifying what it hashed.

    strict and out_dir are not optional either: two runs under different
    policies would otherwise produce indistinguishable manifests, and "was
    --strict on?" is exactly the question a receipt exists to answer.

    checked_board_sha256 is not optional for the same reason. The packaged
    hash alone says what was sent; only the pair says it was the thing that
    passed. require_unchanged makes them equal in any published manifest —
    recording both is what lets a reader confirm that rather than trust it.

    schematic is None for a PCB-only package, which is a real state and gets
    recorded as one rather than left to look like an omission.
    """
    def name_for(p: str) -> str:
        return os.path.relpath(p, package_root)

    return {
        "generated": datetime.now(timezone.utc).isoformat(),
        "kicad_cli_version": cli_version,
        "policy": {"strict": bool(strict), "out_dir": out_dir},
        "board": {"path": os.path.basename(board), "sha256": sha256(board),
                  "checked_sha256": checked_board_sha256},
        "schematic": None if schematic is None else {
            "path": os.path.basename(schematic), "sha256": sha256(schematic),
            "checked_sha256": checked_schematic_sha256},
        # Includes what was excluded and which checks the project file
        # disables: "PASSED" is only meaningful alongside its own coverage.
        "verdict": verdict_json(verdict),
        "files": [{"name": name_for(p), "sha256": sha256(p)} for p in sorted(files)],
    }
