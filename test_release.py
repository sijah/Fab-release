"""Stdlib tests for release.py. No KiCad needed: git, prefab-gate, kicad-cli, the
The fab-data script and the plugins are replaced by FakeTools where they would run."""
import datetime
import hashlib
import json
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

import release
from release import Stop, read_rev, release_name, rev_slug

PCB = ('(kicad_pcb\n  (version 20250114)\n  (title_block\n'
       '    (date "2025-12-10")\n    (rev "Rev 1.41")\n  )\n'
       '\t(layers\n\t\t(0 "F.Cu" signal)\n\t\t(2 "B.Cu" signal)\n\t\t(1 "F.Mask" user)\n'
       '\t\t(3 "B.Mask" user)\n\t\t(5 "F.SilkS" user "F.Silkscreen")\n'
       '\t\t(7 "B.SilkS" user "B.Silkscreen")\n\t\t(25 "Edge.Cuts" user)\n\t)\n)\n')


def fab_files(copper=2):
    """Gerber name -> FileFunction, as kicad-cli writes them for a board with that many copper layers."""
    inner = [(f"In{k}_Cu", f"Copper,L{k + 1},Inr") for k in range(1, copper - 1)]
    return dict([("F_Cu", "Copper,L1,Top"), *inner, ("B_Cu", f"Copper,L{copper},Bot"),
                 ("F_Mask", "Soldermask,Top"), ("B_Mask", "Soldermask,Bot"),
                 ("F_Silkscreen", "Legend,Top"), ("B_Silkscreen", "Legend,Bot"),
                 ("Edge_Cuts", "Profile,NP")])


def write_gerbers(fab, board, copper=2, skip=()):
    fab.mkdir(parents=True, exist_ok=True)
    for name, func in fab_files(copper).items():
        if name not in skip:
            (fab / f"{board}-{name}.gbr").write_text(f"%TF.FileFunction,{func}*%\nM02*\n")
    (fab / f"{board}-job.gbrjob").write_text("{}")


def write_drill(fab, board, copper=2):
    fab.mkdir(parents=True, exist_ok=True)
    (fab / f"{board}-PTH.drl").write_text(f"M48\n; #@! TF.FileFunction,Plated,1,{copper},PTH\n")
    (fab / f"{board}-drl.rpt").write_text("report")
DAY = datetime.date(2026, 10, 14)


class TmpCase(unittest.TestCase):
    def setUp(self):
        t = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(t.cleanup)
        self.tmp = Path(t.name)


def make_project(root):
    """A minimal board folder: board, schematic, project, a sub-sheet, junk that must not stage."""
    files = {"BRD.kicad_pcb": PCB, "BRD.kicad_sch": "(kicad_sch)", "BRD.kicad_pro": "{}",
             "fp-lib-table": "(fp_lib_table)", "ibom.config.ini": "[html_defaults]",
             "notes.txt": "not staged", "sub/Child.kicad_sch": "(kicad_sch child)",
             "BRD-backups/old.kicad_sch": "(old)"}
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    return root / "BRD.kicad_pcb"


def git_repo(root):
    """git init root; returns g(*args) that runs git there with a throwaway identity."""
    root.mkdir(parents=True, exist_ok=True)

    def g(*args):
        return subprocess.run(["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t",
                               *args], check=True, capture_output=True, text=True)
    g("init", "-q")
    return g


IBOM_HTML = "ibom_BRD_2026-10-14_15-49-09.html"


class FakeTools:
    """Stands in for subprocess.run. Answers git, prefab-gate, kicad-cli, the fab-data script and
    the plugins, writing every file the real tool would so release.py's output checks see them.

    fail:   substring of a command line that should exit 1
    silent: substrings of command lines that exit 0 but write nothing
    mutate: `pcb render` edits the staged board (an input changing after the gate)
    """

    def __init__(self, head="abc123", dirty="", tags=(), gate_rc=0, fail=None,
                 silent=(), mutate=False):
        self.head, self.dirty, self.gate_rc = head, dirty, gate_rc
        self.tags = tags if isinstance(tags, dict) else dict.fromkeys(tags, "old9999")  # tag -> commit
        self.fail, self.silent, self.mutate = fail, set(silent), mutate
        self.calls, self.tagged = [], []

    def __call__(self, cmd, **kw):
        self.calls.append(cmd)
        line = " ".join(cmd)

        def done(rc=0, out=""):
            return subprocess.CompletedProcess(cmd, rc, out, "")
        if self.fail and self.fail in line:
            return done(1, f"{self.fail} exploded")
        if cmd[0] == "git":
            verb = cmd[3:]
            if verb == ["rev-parse", "HEAD"]:
                return done(0, self.head + "\n") if self.head else done(128)
            if verb[0] == "status":
                return done(0, self.dirty)
            if verb[0] == "rev-parse":  # tag lookup: refs/tags/<tag>^{commit}
                tag = verb[-1].removeprefix("refs/tags/").removesuffix("^{commit}")
                return done(0, self.tags[tag] + "\n") if tag in self.tags else done(1)
            if verb[0] == "tag":
                self.tagged.append(tuple(verb[1:]))
                return done()
        if cmd[1].endswith("prefab_gate.py"):
            return done(self.gate_rc, json.dumps({"verdict": "BLOCKED" if self.gate_rc else "PASS"}))
        if cmd[0] == "kicad-cli" and cmd[1] == "version":
            return done(0, "10.0.7\n")
        if any(s in line for s in self.silent):
            return done()
        if cmd[0] == "kicad-cli":
            pcb_or_sch = Path(cmd[-1])
            o = release.outputs(pcb_or_sch.parent / "out", pcb_or_sch.stem)
            if self.mutate and cmd[1:3] == ["pcb", "render"]:
                pcb_or_sch.write_text("edited after the gate")
            fab = pcb_or_sch.parent / "out" / "fab"
            if cmd[3] in ("gerbers", "drill"):
                (write_gerbers if cmd[3] == "gerbers" else write_drill)(fab, pcb_or_sch.stem)
                return done()
            targets = [Path(cmd[cmd.index("-o") + 1])]
        elif Path(cmd[1]).name == "generate_interactive_bom.py":
            targets = [Path(cmd[cmd.index("--dest-dir") + 1]) / IBOM_HTML]
        else:  # python fab_exports.py <kind> <board.kicad_pcb> <out>
            targets = [Path(cmd[-1])]
        for t in targets:
            t.parent.mkdir(parents=True, exist_ok=True)
            t.write_text(t.name)
        return done()


FAB, ASM, TEST, DOC = "1_Fabrication", "2_Assembly", "3_Test", "4_Documentation"
EXPECTED = {f"{FAB}/BRD.zip", f"{FAB}/BRD_pcbstack.txt", f"{FAB}/Required_impedance_control.xlsx",
            f"{ASM}/BRD-all-pos.csv", f"{ASM}/pcb_component_data.csv", f"{ASM}/BRD_1.41_141026_BOM.csv",
            f"{ASM}/{IBOM_HTML}", f"{ASM}/BRD_assembly_top.pdf", f"{ASM}/BRD_assembly_bottom.pdf",
            f"{TEST}/BRD-testpoints.csv",
            f"{DOC}/BRD.pdf", f"{DOC}/BRD_F.png", f"{DOC}/BRD_B.png", f"{DOC}/BRD_iso.png",
            f"{DOC}/BRD_1.41_141026.zip"}


SIX = PCB.replace('\t\t(2 "B.Cu"', '\t\t(4 "In1.Cu" signal)\n\t\t(6 "In2.Cu" signal)\n'
                  '\t\t(8 "In3.Cu" signal)\n\t\t(10 "In4.Cu" signal)\n\t\t(2 "B.Cu"')


class Gerbers(TmpCase):
    def check(self, copper=6, skip=(), pcb=SIX):
        fab = self.tmp / "fab"
        write_gerbers(fab, "B", copper, skip)
        write_drill(fab, "B", copper)
        release.check_gerbers(fab, pcb)

    def test_six_layer_set_passes(self):
        self.check()

    def test_layer_missing_from_plot_stops_naming_it(self):
        with self.assertRaises(Stop) as cm:
            self.check(skip=("In3_Cu", "B_Mask"))
        self.assertEqual(cm.exception.code, 5)
        self.assertIn("In3.Cu, B.Mask", str(cm.exception))

    def test_board_grew_to_six_layers_plot_still_four(self):
        with self.assertRaises(Stop) as cm:
            self.check(skip=("In3_Cu", "In4_Cu"))
        self.assertIn("missing In3.Cu, In4.Cu.", str(cm.exception))

    def test_plated_drill_required(self):
        fab = self.tmp / "fab"
        write_gerbers(fab, "B", 6)
        with self.assertRaises(Stop) as cm:
            release.check_gerbers(fab, SIX)
        self.assertIn("plated drill", str(cm.exception))

    def test_x1_comment_attributes_count(self):
        fab = self.tmp / "fab"
        write_gerbers(fab, "B", 2)
        write_drill(fab, "B", 2)
        for g in fab.glob("*.gbr"):
            g.write_text(g.read_text().replace("%TF.", "G04 #@! TF.").replace("*%", "*"))
        release.check_gerbers(fab, PCB)

    def test_unplotted_layers_from_plot_settings(self):
        # bits are layer ids: 0 F.Cu, 1 F.Mask, 2 B.Cu, 3 B.Mask, 4 In1, 5 F.SilkS, 6 In2,
        # 7 B.SilkS, 8 In3, 10 In4, 25 Edge.Cuts. In2 (6) and Edge.Cuts (25) left out.
        sel = sum(1 << b for b in (0, 1, 2, 3, 4, 5, 7, 8, 10))
        pcb = SIX + f"(pcbplotparams (layerselection 0x00000000_00000000_{sel >> 32:08x}_{sel & 0xffffffff:08x}))"
        self.assertEqual(release.unplotted_layers(pcb), ["In2.Cu", "Edge.Cuts"])
        self.assertEqual(release.copper_layers(pcb), ["F.Cu", "In1.Cu", "In2.Cu", "In3.Cu", "In4.Cu", "B.Cu"])


class Dialog(TmpCase):
    def test_groups_cover_every_output_once(self):
        keys = [k for _, ks in release.GROUPS for k in ks]
        self.assertEqual(sorted(keys), sorted(release.OUTPUTS))
        self.assertEqual([f for f, _ in release.GROUPS], [FAB, ASM, TEST, DOC])

    def test_board_rev_resolves_variables(self):
        pcb = self.tmp / "B.kicad_pcb"
        (self.tmp / "B.kicad_pro").write_text('{"text_variables": {"REVISION": "V2.0"}}')
        self.assertEqual(release.board_rev(pcb, PCB.replace("Rev 1.41", "${REVISION}")), ("V2.0", "V2.0"))
        self.assertEqual(release.board_rev(pcb, PCB), ("Rev 1.41", "1.41"))
        with self.assertRaises(Stop):
            release.board_rev(pcb, PCB.replace("Rev 1.41", "${NOPE}"))

    def test_release_target_and_heads_up_on_existing_folder_or_zip(self):
        pcb, cfg = self.tmp / "BRD.kicad_pcb", {**release.DEFAULTS, "folder": "rel"}
        name, final = release.release_target(pcb, cfg, "1.41", DAY)
        self.assertEqual((name, final), ("BRD_1.41_141026", self.tmp / "rel" / "BRD_1.41_141026"))
        with mock.patch.object(release, "run", FakeTools()):
            self.assertEqual(release.heads_up(self.tmp, final, "t", "abc123"), [])
            for made in (final, final.with_name(f"{name}.zip")):
                made.parent.mkdir(parents=True, exist_ok=True)
                made.mkdir() if made == final else made.write_text("z")
                self.assertIn("Recycle Bin", release.heads_up(self.tmp, final, "t", "abc123")[0])
                shutil.rmtree(final, ignore_errors=True)

    def test_heads_up_on_existing_tag(self):
        final = self.tmp / "none"
        with mock.patch.object(release, "run", FakeTools(tags={"t": "abc123"})):
            self.assertIn("re-run", release.heads_up(self.tmp, final, "t", "abc123")[0])
            self.assertIn("older commit (abc123)", release.heads_up(self.tmp, final, "t", "def456")[0])

    def test_tag_name(self):
        self.assertEqual(release.tag_name("BRD", "1.41"), "release/BRD/1.41")


class Naming(unittest.TestCase):
    def test_read_rev_from_title_block(self):
        self.assertEqual(read_rev(PCB), "Rev 1.41")

    def test_read_rev_missing_is_empty(self):
        self.assertEqual(read_rev('(kicad_pcb (title_block (date "x")))'), "")
        self.assertEqual(read_rev("(kicad_pcb)"), "")

    def test_slug_drops_rev_prefix(self):
        for rev, slug in [("Rev 1.41", "1.41"), ("Rev1.41", "1.41"), ("rev.2", "2"),
                          ("REV-A", "A"), ("Revision B", "B"), ("1.41", "1.41"),
                          ("V1.41", "V1.41"), ("R1", "R1")]:
            with self.subTest(rev=rev):
                self.assertEqual(rev_slug(rev), slug)

    def test_slug_refuses_empty(self):
        for rev in ("", "   ", "Rev", "Rev "):
            with self.subTest(rev=rev):
                with self.assertRaises(Stop) as cm:
                    rev_slug(rev)
                self.assertEqual(cm.exception.code, 4)

    def test_slug_refuses_folder_or_tag_unsafe(self):
        for rev in ("1/2", "A B", "1:0", "v1~2", "a*b"):
            with self.subTest(rev=rev):
                with self.assertRaises(Stop) as cm:
                    rev_slug(rev)
                self.assertEqual(cm.exception.code, 4)

    def test_release_name(self):
        self.assertEqual(release_name("BRD_Ver1.41", "1.41", DAY), "BRD_Ver1.41_1.41_141026")


class Git(TmpCase):
    def committed_project(self):
        g = git_repo(self.tmp)
        proj = self.tmp / "board"
        make_project(proj)
        (self.tmp / "other.txt").write_text("outside the board folder")
        g("add", "-A")
        g("commit", "-qm", "init")
        return g, proj

    def test_clean_repo(self):
        _, proj = self.committed_project()
        commit, dirty = release.git_state(proj)
        self.assertRegex(commit, r"^[0-9a-f]{40}$")
        self.assertEqual(dirty, [])

    def test_dirty_lists_project_files_only(self):
        _, proj = self.committed_project()
        (proj / "BRD.kicad_pcb").write_text("changed")
        (self.tmp / "other.txt").write_text("changed outside")
        _, dirty = release.git_state(proj)
        self.assertEqual(len(dirty), 1)
        self.assertIn("BRD.kicad_pcb", dirty[0])

    def test_local_view_settings_not_dirty(self):
        # KiCad rewrites .kicad_prl (layer visibility, zoom) on every open/close
        _, proj = self.committed_project()
        (proj / "BRD.kicad_prl").write_text("{}")
        self.assertEqual(release.git_state(proj)[1], [])

    def test_release_settings_not_dirty(self):
        # the button saves <board>.release.json on every run; the manifest records what was used
        _, proj = self.committed_project()
        (proj / "BRD.release.json").write_text("{}")
        self.assertEqual(release.git_state(proj)[1], [])

    def test_own_release_output_not_dirty(self):
        # releasing beside the board: the next run must not see the last release as uncommitted
        _, proj = self.committed_project()
        (proj / "BRD_1.41_141026").mkdir()
        (proj / "BRD_1.41_141026" / "manifest.json").write_text("{}")
        (proj / "BRD_1.41_141026.zip").write_text("zip")
        (proj / "BRD_notes_v2.txt").write_text("real work")
        ignore = release.own_output(proj, proj, "BRD", release.NAME)
        _, dirty = release.git_state(proj, ignore)
        self.assertEqual(len(dirty), 1)
        self.assertIn("BRD_notes_v2.txt", dirty[0])

    def test_not_a_repo(self):
        proj = self.tmp / "plain"
        make_project(proj)
        self.assertEqual(release.git_state(proj), (None, []))

    def test_tag_exists(self):
        g, proj = self.committed_project()
        g("tag", "release/BRD/1.41")
        head = g("rev-parse", "HEAD").stdout.strip()
        self.assertEqual(release.tag_commit(proj, "release/BRD/1.41"), head)
        self.assertIsNone(release.tag_commit(proj, "release/BRD/9"))


class Staging(TmpCase):
    def test_stages_project_files_and_sub_sheets_only(self):
        proj = self.tmp / "BRD"
        make_project(proj)
        stale = proj / "release" / "old" / "stale.kicad_sch"
        stale.parent.mkdir(parents=True)
        stale.write_text("(stale)")
        dest = self.tmp / "stage"
        release.stage(proj, dest)
        got = {p.relative_to(dest).as_posix() for p in dest.rglob("*") if p.is_file()}
        self.assertEqual(got, {"BRD.kicad_pcb", "BRD.kicad_sch", "BRD.kicad_pro",
                               "fp-lib-table", "ibom.config.ini", "sub/Child.kicad_sch"})

    def test_hash_inputs_covers_boards_and_sheets(self):
        proj = self.tmp / "BRD"
        make_project(proj)
        dest = self.tmp / "stage"
        release.stage(proj, dest)
        before = release.hash_inputs(dest)
        self.assertEqual(set(before), {"BRD.kicad_pcb", "BRD.kicad_sch", "sub/Child.kicad_sch"})
        (dest / "sub" / "Child.kicad_sch").write_text("(edited)")
        self.assertNotEqual(release.hash_inputs(dest), before)


class Plugins(TmpCase):
    def make(self, plugins, *names):
        for n in names:
            (plugins / n).mkdir(parents=True)

    def lookup(self, env=None, docs="nowhere"):
        return release.plugins_dir(env=env or {}, documents=lambda: self.tmp / docs,
                                   here=self.tmp / "repo" / "fab-release")

    def test_env_override(self):
        self.make(self.tmp / "third" / "plugins", release.IBOM)
        got = self.lookup(env={"KICAD10_3RD_PARTY": str(self.tmp / "third")})
        self.assertEqual(got, self.tmp / "third" / "plugins")

    def test_installed_beside_the_other_plugins(self):
        plugins = self.tmp / "plugins"
        self.make(plugins, release.IBOM, "com_github_sijah_fab-release")
        got = release.plugins_dir(env={}, documents=lambda: self.tmp / "nowhere",
                                  here=plugins / "com_github_sijah_fab-release")
        self.assertEqual(got, plugins)

    def test_redirected_documents(self):
        plugins = self.tmp / "OneDrive" / "Documents" / "KiCad" / "10.0" / "3rdparty" / "plugins"
        self.make(plugins, release.IBOM)
        self.assertEqual(self.lookup(docs="OneDrive/Documents"), plugins)

    def test_missing_plugin_named(self):
        self.make(self.tmp / "nowhere" / "KiCad" / "10.0" / "3rdparty" / "plugins", "other")
        with self.assertRaises(Stop) as cm:
            self.lookup()
        self.assertEqual(cm.exception.code, 3)
        self.assertIn(release.IBOM, str(cm.exception))
        self.assertIn("nowhere", str(cm.exception))


class Build(TmpCase):
    def build(self, pcb=PCB, cfg=None, **fake):
        self.tools = FakeTools(**fake)
        self.st, self.partial = self.tmp / "stage", self.tmp / "release" / ".x.partial"
        self.st.mkdir()
        (self.st / "BRD.kicad_pcb").write_text(pcb)
        with mock.patch.object(release, "run", self.tools):
            release.build(self.st, self.partial, "BRD", "BRD_1.41_141026",
                          Path("kicad-cli"), Path("python"), Path("plugins"),
                          cfg or release.DEFAULTS)

    def test_extra_fab_files_copied_to_fabrication(self):
        a, b = self.tmp / "imp.xlsx", self.tmp / "notes.txt"
        a.write_bytes(b"xlsx")
        b.write_text("n")
        self.build(cfg={**release.DEFAULTS, "fab_extra": f"{a} ; {b}"})
        self.assertEqual((self.partial / FAB / "imp.xlsx").read_bytes(), b"xlsx")
        self.assertTrue((self.partial / FAB / "notes.txt").is_file())

    def stop(self, **fake):
        with self.assertRaises(Stop) as cm:
            self.build(**fake)
        self.assertEqual(cm.exception.code, 5)
        return str(cm.exception)

    def test_lays_out_release(self):
        self.build()
        got = {p.relative_to(self.partial).as_posix()
               for p in self.partial.rglob("*") if p.is_file()}
        self.assertEqual(got, EXPECTED)
        gerbers = set(zipfile.ZipFile(self.partial / FAB / "BRD.zip").namelist())
        self.assertEqual(gerbers, {f"BRD-{n}.gbr" for n in fab_files()}
                         | {"BRD-job.gbrjob", "BRD-PTH.drl", "BRD-drl.rpt", "BRD_pcbstack.txt"})
        step_zip = zipfile.ZipFile(self.partial / DOC / "BRD_1.41_141026.zip").namelist()
        self.assertEqual(step_zip, ["BRD.step"])

    def test_gerbers_missing_a_board_copper_layer_stop(self):
        four = PCB.replace('\t\t(2 "B.Cu"', '\t\t(4 "In1.Cu" signal)\n\t\t(6 "In2.Cu" signal)\n\t\t(2 "B.Cu"')
        msg = self.stop(pcb=four)  # the fake plots 2 copper layers, the board has 4
        self.assertIn("In1.Cu, In2.Cu", msg)
        self.assertFalse(self.partial.exists())

    def test_fab_data_runs_without_kicad_python(self):
        # no pcbnew/SWIG: our own python runs it, KiCad's is only for iBOM
        self.build()
        fab = [c for c in self.tools.calls if c[1].endswith("fab_exports.py")]
        self.assertEqual(sorted(c[2] for c in fab), ["components", "stackup", "testpoints"])
        self.assertTrue(all(c[0] == sys.executable for c in fab))

    def bom_cmd(self, **cfg):
        cmds = release.export_commands(Path("kicad-cli"), self.tmp, "BRD", {**release.DEFAULTS, **cfg})
        return dict((label, cmd) for _, label, cmd, _ in cmds)["BOM"]

    def test_bom_preset_replaces_field_lists(self):
        # a BOM preset saved in the schematic (Symbol Fields Table > View presets)
        cmd = self.bom_cmd(bom_preset="assembly")
        self.assertEqual(cmd[cmd.index("--preset") + 1], "assembly")
        self.assertNotIn("--fields", cmd)
        self.assertNotIn("--group-by", cmd)

    def test_no_bom_preset_uses_field_lists(self):
        cmd = self.bom_cmd()
        self.assertNotIn("--preset", cmd)
        self.assertEqual(cmd[cmd.index("--fields") + 1], release.DEFAULTS["bom_fields"])

    def test_assembly_pdf_excludes_values(self):
        cmds = dict((label, cmd) for _, label, cmd, _ in
                    release.export_commands(Path("kicad-cli"), self.tmp, "BRD"))
        self.assertIn("--exclude-value", cmds["assembly PDF top"])
        self.assertIn("--exclude-value", cmds["assembly PDF bottom"])

    def test_isometric_render(self):
        cmds = release.export_commands(Path("kicad-cli"), self.tmp, "BRD")
        iso = dict((label, cmd) for _, label, cmd, _ in cmds)["render isometric"]
        self.assertEqual(iso[iso.index("--rotate") + 1], "-45,0,45")

    def test_position_file_uses_drill_origin(self):
        # A hand-made POS is relative to the drill/place origin, like the test points
        pos = dict((label, cmd) for _, label, cmd, _ in
                   release.export_commands(Path("kicad-cli"), self.tmp, "BRD"))["position"]
        self.assertIn("--use-drill-file-origin", pos)

    def test_drill_origin_follows_gerber_plot_setting(self):
        drill = lambda: dict((label, cmd) for _, label, cmd, _ in  # noqa: E731
                             release.export_commands(Path("kicad-cli"), self.tmp, "BRD"))["drill"]
        self.assertEqual(drill()[drill().index("--drill-origin") + 1], "absolute")
        (self.tmp / "BRD.kicad_pcb").write_text("(pcbplotparams (useauxorigin yes))", encoding="utf-8")
        self.assertEqual(drill()[drill().index("--drill-origin") + 1], "plot")

    def test_export_failure_stops(self):
        self.assertIn("pcb render exploded", self.stop(fail="pcb render"))

    def test_missing_export_output_stops(self):
        self.assertIn("BRD_assembly_top.pdf", self.stop(silent={"F.CrtYd"}))

    def test_assembly_pdf_plots_a_copy_with_references_shown(self):
        hidden = PCB.replace("\n)\n", '\n\t(footprint "R"\n\t\t(property "Reference" "R1"\n\t\t\t(layer "F.SilkS")'
                             '\n\t\t\t(hide yes)\n\t\t\t(effects (font (size 1 1)))\n\t\t)\n\t\t(property "Value" "10k"'
                             '\n\t\t\t(layer "F.Fab")\n\t\t\t(hide yes)\n\t\t)\n\t)\n)\n')
        self.build(pcb=hidden)
        pdfs = [c for c in self.tools.calls if c[1:4] == ["pcb", "export", "pdf"]]
        self.assertEqual([c[c.index("--layers") + 1] for c in pdfs],
                         ["F.SilkS,F.CrtYd,Edge.Cuts", "B.SilkS,B.CrtYd,Edge.Cuts"])
        self.assertTrue(all("--black-and-white" in c and c[c.index("--scale") + 1] == "0" for c in pdfs))
        copy = Path(pdfs[0][-1])
        self.assertNotEqual(copy, self.st / "BRD.kicad_pcb")  # the gate-checked board is not plotted
        text = copy.read_text(encoding="utf-8")
        self.assertNotIn('"R1"\n\t\t\t(layer "F.SilkS")\n\t\t\t(hide yes)', text)
        self.assertIn('"10k"\n\t\t\t(layer "F.Fab")\n\t\t\t(hide yes)', text)  # only references shown
        self.assertEqual((self.st / "BRD.kicad_pcb").read_text(), hidden)
        self.assertEqual(release.hash_inputs(self.st), {"BRD.kicad_pcb": release.sha256(self.st / "BRD.kicad_pcb")})

    def test_show_references_skips_quoted_parens(self):
        text = '(fp (property "Reference" "R(1\\")" (hide yes) (effects (font))) (property "Value" "x" (hide yes)))'
        self.assertEqual(release.show_references(text),
                         '(fp (property "Reference" "R(1\\")" (effects (font))) (property "Value" "x" (hide yes)))')

    def test_script_writing_nothing_stops(self):
        self.assertIn("BRD_pcbstack.txt", self.stop(silent={" stackup "}))

    def test_ibom_writing_nothing_stops(self):
        self.assertIn("iBOM", self.stop(silent={"generate_interactive_bom"}))

    def test_gate_block_lists_findings_readably(self):
        verdict = {"passed": False, "cosmetic": [{}, {}], "ignored_checks": [{"key": "text_height"}],
                   "blocking": [{"type": "shorting_items", "description": "Items shorting two nets",
                                 "items": ["Polygon of U1 on F.Cu", "Track [ANT] on F.Cu"]},
                                {"type": "footprint_type_mismatch", "description": "Type mismatch",
                                 "items": ["Footprint U2"]}]}

        def gate(cmd, **kw):
            return subprocess.CompletedProcess(cmd, 2, json.dumps(verdict),
                                               "Note: refilling updated the zone fills in C:\\tmp")
        with mock.patch.object(release, "run", gate), self.assertRaises(Stop) as cm:
            release.run_gate(Path("BRD.kicad_pcb"), Path("kicad-cli"))
        msg = str(cm.exception)
        self.assertEqual(cm.exception.code, 2)
        for text in ("Items shorting two nets", "Polygon of U1 on F.Cu", "Footprint U2",
                     "2 blocking", "2 cosmetic"):
            self.assertIn(text, msg)
        for noise in ("text_height", "refilling", "{"):
            self.assertNotIn(noise, msg)

    def test_gate_verdicts(self):
        for rc, code in ((2, 2), (3, 3), (1, 3)):
            with self.subTest(rc=rc), mock.patch.object(release, "run", FakeTools(gate_rc=rc)):
                with self.assertRaises(Stop) as cm:
                    release.run_gate(Path("BRD.kicad_pcb"), Path("kicad-cli"))
                self.assertEqual(cm.exception.code, code)
        with mock.patch.object(release, "run", FakeTools()):
            self.assertEqual(release.run_gate(Path("BRD.kicad_pcb"), Path("kicad-cli")),
                             {"verdict": "PASS"})


class Config(TmpCase):
    def test_defaults_without_a_file(self):
        self.assertEqual(release.load_config(self.tmp, "BRD"), release.DEFAULTS)

    def test_file_overrides_and_keeps_the_rest(self):
        (self.tmp / "BRD.release.json").write_text(json.dumps(
            {"outputs": {"step": False}, "name": "{board}-R{rev}", "gate": {"strict": True}}))
        cfg = release.load_config(self.tmp, "BRD")
        self.assertFalse(cfg["outputs"]["step"])
        self.assertTrue(cfg["outputs"]["ibom"])
        self.assertEqual(cfg["name"], "{board}-R{rev}")
        self.assertEqual(cfg["gate"], {"strict": True, "parity": True, "ampacity": True})
        self.assertEqual(cfg["bom_fields"], release.DEFAULTS["bom_fields"])

    def test_bad_settings_refused(self):
        for bad in ({"outputz": {}}, {"outputs": {"panel": True}}, {"outputs": {"step": "no"}},
                    {"name": 5}, {"gate": {"strict": "yes"}}):
            with self.subTest(bad=bad):
                (self.tmp / "BRD.release.json").write_text(json.dumps(bad))
                with self.assertRaises(Stop) as cm:
                    release.load_config(self.tmp, "BRD")
                self.assertEqual(cm.exception.code, 4)
        (self.tmp / "BRD.release.json").write_text("{not json")
        with self.assertRaises(Stop):
            release.load_config(self.tmp, "BRD")

    def test_save_round_trip(self):
        cfg = release.load_config(self.tmp, "BRD")
        cfg["outputs"]["renders"] = False
        cfg["folder"] = "D:/Releases"
        release.save_config(self.tmp, "BRD", cfg)
        self.assertEqual(release.load_config(self.tmp, "BRD"), cfg)

    def test_name_pattern(self):
        self.assertEqual(release_name("BRD", "1.41", DAY, "{board}-R{rev}-{date}"), "BRD-R1.41-141026")
        for fmt in ("{foo}", "{board}/{rev}", "", "{board"):
            with self.subTest(fmt=fmt):
                with self.assertRaises(Stop) as cm:
                    release_name("BRD", "1.41", DAY, fmt)
                self.assertEqual(cm.exception.code, 4)


TOOLS = (Path("kicad-cli"), Path("python"), Path("plugins"))


class Release(TmpCase):
    def setUp(self):
        super().setUp()
        self.proj = self.tmp / "BRD"
        self.pcb = make_project(self.proj)
        self.rel = self.proj / "release"
        self.final = self.rel / "BRD_1.41_141026"
        self.configure()  # these tests release into release/; the default is beside the board

    def go(self, tools=None, **kw):
        self.tools = tools or FakeTools()
        with mock.patch.object(release, "run", self.tools), \
                mock.patch.object(release, "find_tools", return_value=TOOLS):
            return release.release(self.pcb, today=DAY, **kw)

    def stop(self, tools=None, **kw):
        with self.assertRaises(Stop) as cm:
            self.go(tools, **kw)
        return cm.exception

    def release_dir(self):
        return sorted(p.name for p in self.rel.iterdir())

    def manifest(self):
        return json.loads((self.final / "manifest.json").read_text(encoding="utf-8"))

    def test_publishes_folder_zip_manifest_and_tag(self):
        self.assertEqual(self.go(), self.final)
        got = {p.relative_to(self.final).as_posix() for p in self.final.rglob("*") if p.is_file()}
        self.assertEqual(got, EXPECTED | {"manifest.json"})
        names = zipfile.ZipFile(self.rel / "BRD_1.41_141026.zip").namelist()
        self.assertIn(f"BRD_1.41_141026/{ASM}/BRD_1.41_141026_BOM.csv", names)
        self.assertIn("BRD_1.41_141026/manifest.json", names)
        m = self.manifest()
        self.assertEqual((m["git_commit"], m["dirty"], m["rev"]), ("abc123", False, "Rev 1.41"))
        self.assertEqual(set(m["staged_sha256"]), {"BRD.kicad_pcb", "BRD.kicad_sch", "sub/Child.kicad_sch"})
        self.assertEqual(m["source_board_sha256"], release.sha256(self.pcb))
        self.assertEqual(set(m["files"]), EXPECTED)
        self.assertEqual(m["gate"], {"verdict": "PASS"})
        self.assertEqual(m["tool_version"], release.VERSION)
        self.assertEqual(self.tools.tagged, [("release/BRD/1.41", "abc123")])
        self.assertEqual((self.rel / ".gitignore").read_text(), "*\n")
        self.assertEqual(self.release_dir(), [".gitignore", "BRD_1.41_141026", "BRD_1.41_141026.zip"])

    def test_project_files_untouched(self):
        def snapshot():
            return {p.relative_to(self.proj).as_posix(): release.sha256(p)
                    for p in self.proj.rglob("*") if p.is_file() and "release" not in p.parts}
        before = snapshot()
        self.go()
        self.assertEqual(snapshot(), before)

    def test_gate_blocked_publishes_nothing(self):
        self.assertEqual(self.stop(FakeTools(gate_rc=2)).code, 2)
        self.assertEqual(self.release_dir(), [".gitignore"])
        self.assertEqual(self.tools.tagged, [])

    def test_gate_cannot_run(self):
        self.assertEqual(self.stop(FakeTools(gate_rc=3)).code, 3)

    def test_failed_export_publishes_nothing(self):
        self.assertEqual(self.stop(FakeTools(fail="pcb render")).code, 5)
        self.assertEqual(self.release_dir(), [".gitignore"])

    def test_staged_input_changed_publishes_nothing(self):
        self.assertEqual(self.stop(FakeTools(mutate=True)).code, 3)
        self.assertEqual(self.release_dir(), [".gitignore"])

    def test_dirty_refused_before_anything_written(self):
        e = self.stop(FakeTools(dirty=" M BRD/BRD.kicad_pcb\n"))
        self.assertEqual(e.code, 4)
        self.assertIn("BRD.kicad_pcb", str(e))
        self.assertFalse(self.rel.exists())

    def test_only_uncommitted_refusals_offer_allow_dirty(self):
        # the toolbar button shows "Release anyway" when the refusal names --allow-dirty
        self.assertIn("--allow-dirty", str(self.stop(FakeTools(dirty=" M BRD/BRD.kicad_pcb\n"))))
        self.assertIn("--allow-dirty", str(self.stop(FakeTools(head=""))))
        (self.proj / "BRD.kicad_sch").unlink()
        self.assertNotIn("--allow-dirty", str(self.stop()))

    def test_allow_dirty_publishes_marked_and_untagged(self):
        self.go(FakeTools(dirty=" M BRD/BRD.kicad_pcb\n"), allow_dirty=True)
        self.assertTrue(self.manifest()["dirty"])
        self.assertEqual(self.tools.tagged, [])

    def test_not_a_repo(self):
        self.assertEqual(self.stop(FakeTools(head="")).code, 4)
        self.go(FakeTools(head=""), allow_dirty=True)
        self.assertIsNone(self.manifest()["git_commit"])
        self.assertEqual(self.tools.tagged, [])

    def test_existing_release_replaced_old_to_recycle_bin(self):
        self.final.mkdir(parents=True)
        (self.final / "old.txt").write_text("earlier release")
        zip_final = self.final.with_name(f"{self.final.name}.zip")
        zip_final.write_text("old zip")
        binned = []
        with mock.patch.object(release, "trash", lambda p: (binned.append(p), shutil.rmtree(p)
                                                            if p.is_dir() else p.unlink(missing_ok=True))):
            self.go()
        self.assertEqual(binned, [self.final, zip_final])
        self.assertFalse((self.final / "old.txt").exists())
        self.assertTrue((self.final / "manifest.json").is_file())

    def test_trash_failure_keeps_old_release(self):
        self.final.mkdir(parents=True)
        with mock.patch.object(release, "trash", side_effect=Stop(5, "open")):
            self.assertEqual(self.stop().code, 5)
        self.assertTrue(self.final.is_dir())
        self.assertFalse(any(p.name.endswith(".partial") for p in self.rel.iterdir()))

    def test_existing_tag_released_untagged(self):
        self.go(FakeTools(tags={"release/BRD/1.41": "old9999"}))
        self.assertEqual(self.tools.tagged, [])
        self.assertEqual(self.manifest()["rerelease_of"], {"tag": "release/BRD/1.41", "commit": "old9999"})

    def test_no_tag(self):
        self.go(tag=False)
        self.assertEqual(self.tools.tagged, [])

    def test_stale_partial_cleared(self):
        junk = self.rel / ".BRD_1.41_141026.partial" / "junk.txt"
        junk.parent.mkdir(parents=True)
        junk.write_text("from a crashed run")
        self.go()
        self.assertFalse(junk.parent.exists())
        self.assertFalse((self.final / "junk.txt").exists())

    def test_missing_schematic_refused(self):
        (self.proj / "BRD.kicad_sch").unlink()
        self.assertEqual(self.stop().code, 4)

    def test_missing_rev_refused(self):
        self.pcb.write_text('(kicad_pcb (title_block (date "x")))')
        self.assertEqual(self.stop().code, 4)

    def configure(self, **cfg):
        cfg.setdefault("folder", "release")
        (self.proj / "BRD.release.json").write_text(json.dumps(cfg))

    def test_default_extra_fab_file_is_shipped(self):
        (f,) = release.extra_files(release.DEFAULTS)
        self.assertTrue(f.is_file(), f)
        self.assertEqual(f.parent, release.FAB_FILES)

    def test_missing_extra_fab_file_stops_before_anything(self):
        self.configure(fab_extra=str(self.tmp / "nope.xlsx"))
        e = self.stop()
        self.assertEqual(e.code, 3)
        self.assertIn("nope.xlsx", str(e))
        self.assertFalse(self.rel.exists())

    def test_only_ticked_outputs_made(self):
        self.configure(outputs={"step": False, "renders": False, "ibom": False, "gerber": False})
        self.go()
        got = {p.relative_to(self.final).as_posix() for p in self.final.rglob("*") if p.is_file()}
        skipped = {f"{DOC}/BRD_1.41_141026.zip", f"{DOC}/BRD_F.png", f"{DOC}/BRD_B.png", f"{DOC}/BRD_iso.png",
                   f"{ASM}/{IBOM_HTML}", f"{FAB}/BRD.zip", f"{FAB}/BRD_pcbstack.txt"}
        self.assertEqual(got, (EXPECTED - skipped) | {"manifest.json"})
        line = "\n".join(" ".join(map(str, c)) for c in self.tools.calls)
        for never in ("export step", "render", "generate_interactive_bom", "gerbers", " stackup "):
            self.assertNotIn(never, line)
        self.assertFalse(self.manifest()["config"]["outputs"]["step"])

    def test_nothing_ticked_refused(self):
        self.configure(outputs={k: False for k in release.DEFAULTS["outputs"]})
        self.assertEqual(self.stop().code, 4)

    def test_bom_columns_from_settings(self):
        self.configure(bom_fields="Reference,Value,LCSC", bom_labels="Ref,Val,LCSC",
                       bom_group_by="Value,LCSC")
        self.go()
        bom = next(c for c in self.tools.calls if "bom" in c)
        for arg in ("Reference,Value,LCSC", "Ref,Val,LCSC", "Value,LCSC"):
            self.assertIn(arg, bom)

    def test_folder_and_name_from_settings(self):
        out = self.tmp / "Releases"
        self.configure(folder=str(out), name="{board}-R{rev}")
        self.assertEqual(self.go(), out / "BRD-R1.41")
        self.assertTrue((out / "BRD-R1.41.zip").exists())
        self.assertFalse((out / ".gitignore").exists())  # not ours: outside the project
        self.assertFalse(self.rel.exists())

    def test_relative_folder_is_beside_the_board(self):
        self.configure(folder="fab_out")
        self.assertEqual(self.go(), self.proj / "fab_out" / "BRD_1.41_141026")
        self.assertEqual((self.proj / "fab_out" / ".gitignore").read_text(), "*\n")

    def test_gate_options_from_settings(self):
        self.configure(gate={"strict": True, "parity": False, "ampacity": False})
        self.go()
        gate = next(c for c in self.tools.calls if c[1].endswith("prefab_gate.py"))
        for flag in ("--strict", "--no-parity", "--no-ampacity"):
            self.assertIn(flag, gate)

    def test_default_releases_into_manufacturing(self):
        (self.proj / "BRD.release.json").unlink()
        mfg = self.proj / "Manufacturing"
        self.assertEqual(self.go(), mfg / "BRD_1.41_141026")
        self.assertTrue((mfg / "BRD_1.41_141026.zip").is_file())
        self.assertEqual((mfg / ".gitignore").read_text(), "*\n")
        self.assertFalse((self.proj / ".gitignore").exists())  # never a '*' beside the design

    def test_beside_board_still_possible(self):
        self.configure(folder=".")
        self.assertEqual(self.go(), self.proj / "BRD_1.41_141026")
        self.assertFalse((self.proj / ".gitignore").exists())

    def test_board_folder_gitignore_untouched(self):
        (self.proj / ".gitignore").write_text("*-backups/*\n")
        self.configure(folder=str(self.proj))
        self.go()
        self.assertEqual((self.proj / ".gitignore").read_text(), "*-backups/*\n")

    def test_existing_gitignore_in_release_folder_kept(self):
        (self.rel).mkdir()
        (self.rel / ".gitignore").write_text("mine\n")
        self.go()
        self.assertEqual((self.rel / ".gitignore").read_text(), "mine\n")

    def test_rev_text_variable_resolved_from_project(self):
        self.pcb.write_text(PCB.replace("Rev 1.41", "${REVISION}"))
        (self.proj / "BRD.kicad_pro").write_text(json.dumps({"text_variables": {"REVISION": "Rev 1.41"}}))
        self.assertEqual(self.go(), self.final)

    def test_undefined_rev_variable_refused(self):
        self.pcb.write_text(PCB.replace("Rev 1.41", "${REVISION}"))
        e = self.stop()
        self.assertEqual(e.code, 4)
        self.assertIn("REVISION", str(e))

    def test_planned_steps_match_what_release_prints(self):
        # the button's progress window counts these; they must be the lines release prints
        import contextlib
        import io
        for outputs in ({}, {"step": False, "ibom": False, "gerber": False}):
            with self.subTest(outputs=outputs):
                shutil.rmtree(self.proj / "release", ignore_errors=True)
                self.configure(outputs=outputs)
                printed = io.StringIO()
                with contextlib.redirect_stdout(printed):
                    self.go(tag=False)
                lines = [ln.strip() for ln in printed.getvalue().splitlines() if ln.startswith("  ")]
                cfg = release.load_config(self.proj, "BRD")
                self.assertEqual(lines, release.planned_steps("BRD", cfg))

    def test_ibom_unticked_needs_no_plugin(self):
        self.configure(outputs={"ibom": False})
        with mock.patch.object(release, "run", FakeTools()), \
                mock.patch.object(release, "find_tools", return_value=TOOLS[:2] + (None,)) as find:
            release.release(self.pcb, today=DAY)
        find.assert_called_once_with(ibom=False)
        self.assertTrue(self.final.is_dir())

    def test_main_exit_codes(self):
        with self.assertRaises(SystemExit) as cm:
            release.main([])
        self.assertEqual(cm.exception.code, 3)
        with mock.patch.object(release, "run", FakeTools(gate_rc=2)), \
                mock.patch.object(release, "find_tools", return_value=TOOLS):
            self.assertEqual(release.main([str(self.pcb)]), 2)


@unittest.skipUnless((release.HERE / "prefab-gate" / "scripts" / "prefab_gate.py").exists(),
                     "prefab-gate not cloned; the package bundles it")
class Pcm(TmpCase):
    def setUp(self):
        super().setUp()
        sys.path.insert(0, str(release.HERE / "pcm"))
        import build_pcm
        self.z = zipfile.ZipFile(build_pcm.build(self.tmp))
        self.addCleanup(self.z.close)
        self.names = set(self.z.namelist())

    def test_pcm_layout(self):
        for name in ("metadata.json", "resources/icon.png", "plugins/plugin.json",
                     "plugins/ipc_entry.py", "plugins/requirements.txt",
                     "plugins/icon24.png", "plugins/icon48_dark.png", "plugins/icon64.png",
                     "plugins/release.py",
                     "plugins/scripts/fab_exports.py", "plugins/fab/Required_impedance_control.xlsx", "plugins/prefab_gate/prefab_gate.py",
                     "plugins/prefab_gate/gate/kicad.py", "plugins/prefab_gate/LICENSE"):
            with self.subTest(name=name):
                self.assertIn(name, self.names)
        self.assertEqual([n for n in self.names if "__pycache__" in n or n.endswith(".pyc")], [])
        # an __init__.py would make KiCad also load the folder as a SWIG action plugin
        self.assertNotIn("plugins/__init__.py", self.names)

    def test_version_shipped_and_read(self):
        meta = json.loads(self.z.read("metadata.json"))
        version = meta["versions"][0]["version"]
        self.assertEqual(self.z.read("plugins/VERSION").decode(), version)
        self.assertEqual(release.VERSION, version)  # in the repo: from pcm/metadata.json

    def test_button_is_an_ipc_api_plugin(self):
        """KiCad 10 IPC API (kicad-python), not the SWIG pcbnew bindings."""
        p = json.loads(self.z.read("plugins/plugin.json"))
        self.assertEqual(p["$schema"], "https://go.kicad.org/api/schemas/v1")
        self.assertEqual(p["runtime"]["type"], "python")
        self.assertRegex(p["identifier"], r"^[a-zA-Z][-_a-zA-Z0-9.]{0,98}[a-zA-Z0-9]$")
        (action,) = p["actions"]
        self.assertTrue(action["show-button"])
        self.assertEqual(action["scopes"], ["pcb"])
        for name in [action["entrypoint"], *action["icons-light"], *action["icons-dark"]]:
            self.assertIn(f"plugins/{name}", self.names)
        self.assertIn("kicad-python", self.z.read("plugins/requirements.txt").decode())
        entry = self.z.read(f"plugins/{action['entrypoint']}").decode()
        self.assertIn("from kipy", entry)
        self.assertNotIn("import pcbnew", entry)

    def test_metadata_is_a_kicad10_plugin(self):
        m = json.loads(self.z.read("metadata.json"))
        self.assertEqual((m["identifier"], m["type"], m["versions"][0]["kicad_version"]),
                         ("com.github.sijah.fab-release", "plugin", "10.0"))
        for key in ("name", "description", "description_full", "author", "license", "resources"):
            self.assertIn(key, m)

    def test_icons_are_pngs_of_the_right_size(self):
        for name, size in (("resources/icon.png", 64), ("plugins/icon24.png", 24), ("plugins/icon48.png", 48),
                           ("plugins/icon24_dark.png", 24), ("plugins/icon48_dark.png", 48)):
            data = self.z.read(name)
            self.assertEqual(data[:8], b"\x89PNG\r\n\x1a\n")
            self.assertEqual(struct.unpack(">II", data[16:24]), (size, size))

    def test_submission_describes_the_built_zip(self):
        import build_pcm
        folder = build_pcm.submission(self.z.filename, self.tmp)
        (v,) = json.loads((folder / "metadata.json").read_text(encoding="utf-8"))["versions"]
        data = Path(self.z.filename).read_bytes()
        self.assertEqual((v["download_sha256"], v["download_size"]),
                         (hashlib.sha256(data).hexdigest(), len(data)))
        self.assertTrue(v["download_url"].endswith(f"/v{v['version']}/{Path(self.z.filename).name}"))
        self.assertEqual(v["platforms"], ["windows"])
        self.assertEqual((folder / "icon.png").read_bytes()[:8], b"\x89PNG\r\n\x1a\n")

    def test_installed_release_uses_bundled_gate(self):
        dest = self.tmp / "com_github_sijah_fab-release"
        self.z.extractall(dest)
        r = subprocess.run([sys.executable, "-c", "import release; print(release.GATE)"],
                           cwd=dest / "plugins", capture_output=True, text=True, check=True)
        self.assertEqual(Path(r.stdout.strip()),
                         (dest / "plugins" / "prefab_gate" / "prefab_gate.py").resolve())


sys.path.insert(0, str(release.SCRIPTS))
import fab_exports  # noqa: E402

FAB_PCB = r'''(kicad_pcb (version 20250114) (generator "pcbnew")
  (general (thickness 1.6))
  (setup
    (stackup
      (layer "F.SilkS" (type "Top Silk Screen") (color "White"))
      (layer "F.Mask" (type "Top Solder Mask") (thickness 0.01))
      (layer "F.Cu" (type "copper") (thickness 0.035))
      (layer "dielectric 1" (type "core") (thickness 1.51) (material "FR4") (epsilon_r 4.5)
        (loss_tangent 0.02))
      (layer "B.Cu" (type "copper") (thickness 0.035))
      (dielectric_constraints no))
    (aux_axis_origin 100 50))
  (title_block (title "Demo \"X\"") (rev "Rev 2") (comment 1 "JLC stackup"))
  (footprint "Lib:R_0402" (layer "F.Cu") (at 110 60 90)
    (property "Reference" "R1" (at 0 0 0))
    (attr smd dnp)
    (pad "1" smd rect (at -0.5 0 90) (layers "F.Cu" "F.Mask") (net 1 "/SIG"))
    (pad "2" smd rect (at 0.5 0 90) (layers "F.Cu" "F.Mask") (net 2 "GND")
      (property pad_prop_testpoint)))
  (footprint "Lib:TP" (layer "B.Cu") (at 120 55 270)
    (property "Reference" "TP1")
    (pad "1" thru_hole circle (at 1 0) (layers "*.Cu" "*.Mask") (net "VBAT")
      (property pad_prop_testpoint))
    (pad "" np_thru_hole circle (at 0 2) (layers "*.Cu")))
)
'''
FAB_PRO = {"net_settings": {"netclass_assignments": {"GND": ["Power"]},
                            "netclass_patterns": [{"netclass": "RF", "pattern": "*BAT"}]},
           "board": {"design_settings": {"rules": {"min_track_width": 0.127, "min_clearance": 0.1}}}}


class FabExports(TmpCase):
    """scripts/fab_exports.py: the board file read as text, no pcbnew."""
    board = fab_exports.parse(FAB_PCB)

    def test_components(self):
        rows, total = fab_exports.components(self.board)
        self.assertEqual(rows, [["R1", "110.000", "60.000", "90.0", "Front", "SMD", "R_0402", 2, "Yes"],
                                ["TP1", "120.000", "55.000", "-90.0", "Back", "Mixed", "TP", 2, "No"]])
        self.assertEqual(total, 4)

    def test_testpoints_from_aux_origin_y_up(self):
        got = [(r["source ref des"], r["source pad"], r["net"], r["net class"], r["side"],
                r["x"], r["y"], r["pad type"], r["footprint side"])
               for r in fab_exports.testpoints(self.board, FAB_PRO)]
        self.assertEqual(got, [("R1", "2", "GND", "Power", "TOP", 10.0, -9.5, "SMT", "TOP"),
                               ("TP1", "1", "VBAT", "RF", "BOTTOM", 20.0, -6.0, "THRU", "BOTTOM")])

    def test_no_testpoints_fails(self):
        board = fab_exports.parse(FAB_PCB.replace("(property pad_prop_testpoint)", ""))
        with self.assertRaises(SystemExit):
            fab_exports.write_testpoints(board, {}, self.tmp / "tp.csv")

    def test_stackup(self):
        text = fab_exports.stackup(self.board, FAB_PRO)
        for line in ('\ttitle : Demo "X"', "\tcomment 1 : JLC stackup", "Board Thickness = 1.6 mm",
                     "Copper Layer  = 2 ", "\tF.Cu : 0.035 mm", "\tcore : 1.51 mm : FR4 : 4.5 : 0.02",
                     "Impedance Control : False", "\tTop Silk Screen : White",
                     "\tTop Solder Mask : green : 0.01", "\tMin Track Width : 0.13mm",
                     "\tMin Track Spacing : 0.10mm"):
            with self.subTest(line=line):
                self.assertIn(line + "\n", text)

    def test_net_class_is_every_match_by_priority(self):
        # KiCad 9/10: explicit assignments and all matching patterns combine, highest priority
        # (lowest number) first; patterns may be wildcards or anchored regexes
        pro = {"net_settings": {
            "classes": [{"name": "Default", "priority": 2147483647},
                        {"name": "Power", "priority": 1}, {"name": "RF", "priority": 0}],
            "netclass_assignments": {"GND_RF": ["Power"]},
            "netclass_patterns": [{"netclass": "RF", "pattern": "*_RF"},
                                  {"netclass": "Power", "pattern": "^VB.*"}]}}
        self.assertEqual(fab_exports._net_class("GND_RF", pro), "RF,Power")
        self.assertEqual(fab_exports._net_class("VBAT", pro), "Power")
        self.assertEqual(fab_exports._net_class("XVBAT", pro), "Default")
        self.assertEqual(fab_exports._net_class("BAD", {"net_settings": {
            "netclass_patterns": [{"netclass": "RF", "pattern": "[unclosed"}]}}), "Default")

    def test_testpoint_on_origin_is_plus_zero(self):
        board = fab_exports.parse(FAB_PCB.replace("(at 110 60 90)", "(at 110 50 0)")
                                  .replace('(at 0.5 0 90)', '(at 0 0 90)'))
        r = fab_exports.testpoints(board, {})[0]
        self.assertEqual((str(r["x"]), str(r["y"])), ("10.0", "0.0"))

    def test_missing_project_file_stops(self):
        pcb = self.tmp / "b.kicad_pcb"
        pcb.write_text(FAB_PCB, encoding="utf-8")
        with self.assertRaises(SystemExit) as cm:
            fab_exports.main(["stackup", str(pcb), str(self.tmp / "s.txt")])
        self.assertIn("b.kicad_pro", str(cm.exception))

    def test_dielectric_sublayers_each_listed(self):
        board = fab_exports.parse(FAB_PCB.replace(
            '(loss_tangent 0.02))',
            '(loss_tangent 0.02) addsublayer (thickness 0.2) (material "PP") (epsilon_r 4.1)'
            ' (loss_tangent 0.03))').replace(
            '(layer "B.Cu" (type "copper")',
            '(layer "dielectric 2" (type "prepreg") (material "FR4")) (layer "B.Cu" (type "copper")'))
        text = fab_exports.stackup(board, {})
        self.assertIn("\tcore : 1.51 mm : FR4 : 4.5 : 0.02\n\tcore : 0.2 mm : PP : 4.1 : 0.03\n"
                      "\tprepreg : ? mm : FR4 : ? : ?\n", text)

    def test_title_text_variables_resolved(self):
        board = fab_exports.parse('(kicad_pcb (title_block (title "${NAME}") (rev "${REV}") '
                                  '(comment 1 "PCB: ${STACK} ${UNSET}")))')
        text = fab_exports.stackup(board, {"text_variables": {"NAME": "DEMO", "REV": "V2.0", "STACK": "JLC"}})
        self.assertIn("\ttitle : DEMO\n\trev : V2.0\n\tcomment 1 : PCB: JLC ${UNSET}\n", text)

    def test_string_escapes_and_bom(self):
        self.assertEqual(fab_exports.parse(r'(a "x\ny\"z\\")'), ["a", 'x\ny"z\\'])
        pcb = self.tmp / "bom.kicad_pcb"
        pcb.write_text("﻿" + FAB_PCB, encoding="utf-8")
        self.assertEqual(fab_exports.load(pcb)[0], "kicad_pcb")

    def test_matches_golden_release(self):
        """Byte-for-byte against test points made by the original SWIG plugin, from a real
        board kept out of the repo: .e2e/golden/board.kicad_pcb and testpoints.csv."""
        e2e = release.HERE / ".e2e" / "golden"
        golden = e2e / "testpoints.csv"
        if not golden.is_file():
            self.skipTest("no .e2e/golden board")
        out = self.tmp / "tp.csv"
        fab_exports.main(["testpoints", str(e2e / "board.kicad_pcb"), str(out)])
        self.assertEqual(out.read_bytes(), golden.read_bytes())


if __name__ == "__main__":
    unittest.main()
