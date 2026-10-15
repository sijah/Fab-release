"""Aptener Release toolbar button: KiCad 10 IPC API plugin (see plugin.json).

KiCad starts this script in its own Python process with KICAD_API_SOCKET and
KICAD_API_TOKEN set; kipy.KiCad() uses them to ask the running PCB editor which
board is open. The release itself runs release.py (beside this file) on the board
as saved on disk.
"""
import datetime
import os
import queue
import re
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import wx  # noqa: E402
from kipy.kicad import KiCad  # noqa: E402

from release import (DEFAULTS, FAB, GROUPS, OUTPUTS, VERSION, Stop, board_rev,  # noqa: E402
                     copper_layers, load_config, planned_steps, release_target, save_config,
                     git, heads_up, tag_name, unplotted_layers)

RELEASE = Path(__file__).with_name("release.py")
TITLE = "Aptener Release"
APP_ID = "com.aptener.kicad-release"
ICON = Path(__file__).with_name("icon64.png")
THEMES = ("System", "Light", "Dark")  # names match wx.App.Appearance members (wxPython 4.3+)


def set_icon(win):
    """The logo in the title bar and taskbar (absent when run from the repo)."""
    if ICON.exists():
        win.SetIcon(wx.Icon(str(ICON), wx.BITMAP_TYPE_PNG))


def open_board():
    """Path of the board open in the PCB editor, via the IPC API."""
    board = KiCad().get_board()
    name = Path(board.name)
    return name if name.is_absolute() else Path(board.get_project().path) / name


CANCELLED = -1


def run_release(path, cfg, progress, *flags):
    """release.py in its own process (KiCad stays usable). progress(done, total, msg) names each
    step as release.py prints it; when it returns False, release.py and everything it started is
    killed; nothing is published (a leftover .partial folder is removed by the next run)."""
    steps = planned_steps(path.stem, cfg)
    proc = subprocess.Popen([sys.executable, "-u", str(RELEASE), *flags, str(path)],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            encoding="utf-8", errors="replace",
                            creationflags=subprocess.CREATE_NO_WINDOW)
    lines = queue.Queue()

    def reader():
        for line in proc.stdout:
            lines.put(line)
        lines.put(None)  # end of output
    threading.Thread(target=reader, daemon=True).start()

    out, done, msg = [], 0, "Checking the board, git and the rev..."
    while True:
        try:
            line = lines.get(timeout=0.1)
        except queue.Empty:
            line = ""
        if line is None:
            break
        if line:
            out.append(line)
            if line.startswith("  "):  # a step starting, as release.step() prints it
                done = min(done + 1, len(steps))
                msg = f"Step {done} of {len(steps)}: {line.strip()}"
        # total is one more than the steps, so the bar is never full before the end
        if not progress(done, len(steps) + 1, msg):  # Cancel pressed
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True,
                           creationflags=subprocess.CREATE_NO_WINDOW)
            return subprocess.CompletedProcess(proc.args, CANCELLED, "".join(out), "")
    return subprocess.CompletedProcess(proc.args, proc.wait(), "".join(out), "")


def main():
    if sys.platform == "win32":  # own taskbar entry instead of pythonw.exe
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
    app = wx.App(False)
    # The theme must be set before the first window; wxPython < 4.3 cannot, so no selector.
    config, theme = wx.Config("AptenerRelease"), None
    if hasattr(app, "SetAppearance"):
        theme = config.Read("theme", "System")
        theme = theme if theme in THEMES else "System"
        app.SetAppearance(getattr(wx.App.Appearance, theme))
    try:
        path = open_board()
    except Exception as e:  # no API server, no board open, KiCad closed
        wx.MessageBox(f"Could not get the open board from KiCad:\n\n{e}", TITLE, wx.ICON_ERROR)
        return 1
    if not path.is_file():
        wx.MessageBox(f"Save the board first.\n\n{path}", TITLE, wx.ICON_WARNING)
        return 1
    try:
        cfg = load_config(path.parent, path.stem)
    except Stop as e:
        wx.MessageBox(f"{e}\n\nFix or delete the file and try again.", TITLE, wx.ICON_ERROR)
        return 1
    dlg = SettingsDialog(path, cfg, theme)
    if dlg.ShowModal() == wx.ID_RETRY:  # theme changed: save it, reopen in a fresh process
        config.Write("theme", dlg.theme)
        config.Flush()
        subprocess.Popen([sys.executable, str(Path(__file__).resolve())])
    dlg.Destroy()
    return dlg.code


def release(path, cfg, progress):
    """Run the release, progress shown in the settings dialog, then report the verdict.
    Returns the exit code for main()."""
    r = run_release(path, cfg, progress)
    # Uncommitted changes / no git: offer a trial release (manifest "dirty": true, no tag).
    # Never offered for a gate block.
    if r.returncode == 4 and "--allow-dirty" in r.stdout:
        head, _, files = stop_message(r).partition("\n")
        head = re.sub(r",? or use --allow-dirty for a trial run:?", "", head).rstrip(".:") + "."
        if not report(f"{TITLE}: not committed", head, files.strip(), ok="Release anyway",
                      cancel="Cancel", question="Release anyway as a trial? Its manifest is "
                      "marked dirty and it gets no git tag."):
            return 0
        r = run_release(path, cfg, progress, "--allow-dirty")
    if r.returncode == CANCELLED:
        wx.MessageBox("Release cancelled. Nothing was published.", TITLE, wx.ICON_INFORMATION)
        return 0
    untagged = re.match(r"Released (.+), but git tag failed", stop_message(r)) if r.returncode == 5 else None
    if r.returncode == 0 or untagged:  # untagged: published, only the tag failed (e.g. a git lock)
        folder = untagged[1] if untagged else r.stdout.strip().splitlines()[-1].removeprefix("RELEASED ")
        head = f"Released {Path(folder).name}\n\n{folder}"
        if untagged:
            head += "\n\nIt was published, but the git tag failed. Tag it by hand once git works again."
        if report(f"{TITLE}: released, no git tag" if untagged else TITLE, head,
                  stop_message(r).partition("\n")[2].strip() if untagged else "", ok="Open folder"):
            os.startfile(folder)
        return 0
    head, _, details = stop_message(r).partition("\n")
    verdict = VERDICT.get(r.returncode, f"failed (exit {r.returncode})")
    report(f"{TITLE}: {verdict}", head, details.strip())
    return r.returncode


VERDICT = {2: "blocked by prefab-gate", 3: "could not run", 4: "refused", 5: "a step failed"}


def stop_message(r):
    """What release.py said when it stopped (its "STOP: ..." text). Else (a crash) its last line,
    which names the error, then the end of its output."""
    out = r.stdout + r.stderr
    i = out.rfind("STOP: ")
    if i >= 0:
        return out[i + 6:].strip()
    lines = out.strip().splitlines()[-40:] or ["release.py stopped without saying why."]
    return "\n".join([lines[-1], *lines])


def report(title, headline, details="", ok=None, cancel="Close", question=""):
    """The verdict on top, any details below in a scrollable box that can be copied, e.g. to
    send gate findings to someone, then the question the ok button answers. With a question,
    Enter does not press ok. True when the ok button was pressed."""
    dlg = wx.Dialog(None, title=title, style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
    top = wx.BoxSizer(wx.VERTICAL)
    head = wx.StaticText(dlg, label=headline)
    head.Wrap(620)
    top.Add(head, 0, wx.ALL, 14)
    row = wx.BoxSizer(wx.HORIZONTAL)
    if details:
        box = wx.TextCtrl(dlg, value=details, size=(640, min(260, 60 + 17 * details.count("\n"))),
                          style=wx.TE_MULTILINE | wx.TE_READONLY | wx.HSCROLL | wx.TE_DONTWRAP)
        box.SetFont(wx.Font(wx.FontInfo(9).Family(wx.FONTFAMILY_TELETYPE)))
        top.Add(box, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 14)
        copy = wx.Button(dlg, label="Copy details")

        def on_copy(_):
            if wx.TheClipboard.Open():
                wx.TheClipboard.SetData(wx.TextDataObject(f"{headline}\n\n{details}"))
                wx.TheClipboard.Close()
                copy.SetLabel("Copied")
        copy.Bind(wx.EVT_BUTTON, on_copy)
        row.Add(copy)
    if question:
        ask = wx.StaticText(dlg, label=question)
        ask.Wrap(620)
        top.Add(ask, 0, wx.LEFT | wx.RIGHT | wx.TOP, 14)
    row.AddStretchSpacer()
    if ok:
        yes = wx.Button(dlg, wx.ID_OK, ok)
        row.Add(yes, 0, wx.LEFT, 8)
    no = wx.Button(dlg, wx.ID_CANCEL, cancel)
    row.Add(no, 0, wx.LEFT, 8)
    (no if question or not ok else yes).SetDefault()
    top.Add(row, 0, wx.EXPAND | wx.ALL, 14)
    set_icon(dlg)
    dlg.SetSizerAndFit(top)
    dlg.CentreOnScreen()
    pressed = dlg.ShowModal() == wx.ID_OK
    dlg.Destroy()
    return pressed


def grey(ctrl):
    ctrl.SetForegroundColour(wx.SystemSettings.GetColour(wx.SYS_COLOUR_GRAYTEXT))
    return ctrl


class SettingsDialog(wx.Dialog):
    """What this release makes and where it goes first; the once-per-board settings folded
    away. Saved per board in <board>.release.json (commit it to share)."""

    MORE = (("bom_preset", "BOM preset",
             "A preset saved in the schematic's Symbol Fields Table. When set, the three BOM "
             "boxes below are not used."),
            ("bom_fields", "BOM fields", "KiCad field names, plus ${QUANTITY} and ${DNP}"),
            ("bom_labels", "BOM column titles", "One title per field, in the same order"),
            ("bom_group_by", "BOM group by", "Rows with the same values in these fields merge"),
            ("name", "Name pattern", "{board}, {rev} and {date} (DDMMYY)"),
            ("fab_extra", "Extra fab files", "Copied as-is into 1_Fabrication every release; a bare name is "
             "one shipped with this tool; "
             "separate several with ;"))
    GATE = (("strict", "Strict: cosmetic findings block too"),
            ("parity", "Check schematic parity"),
            ("ampacity", "Check trace current (ampacity)"))
    WRAP = 600

    def __init__(self, path, cfg, theme=None):
        super().__init__(None, title=f"{TITLE} {VERSION}", style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        set_icon(self)
        self.path, self.board, self.shape = path, path.stem, None
        self.code, self.busy, self.cancelled, self.theme = 0, False, False, theme
        top = wx.BoxSizer(wx.VERTICAL)

        # What this release is, and where it lands.
        self.head = wx.StaticText(self, label=self.board)
        self.head.SetFont(self.head.GetFont().Bold().Scaled(1.25))
        top.Add(self.head, 0, wx.LEFT | wx.RIGHT | wx.TOP, 14)
        row = wx.BoxSizer(wx.HORIZONTAL)
        row.Add(wx.StaticText(self, label="Release folder"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)
        self.texts = {"folder": wx.TextCtrl(self, value=cfg["folder"])}
        self.texts["folder"].SetToolTip(". = beside the board, a subfolder of it, or a full path")
        row.Add(self.texts["folder"], 1, wx.ALIGN_CENTER_VERTICAL)
        browse = wx.Button(self, label="Browse...")
        browse.Bind(wx.EVT_BUTTON, self.on_browse)
        row.Add(browse, 0, wx.LEFT, 8)
        top.Add(row, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 14)
        self.target = wx.StaticText(self)
        top.Add(self.target, 0, wx.LEFT | wx.RIGHT | wx.TOP, 14)
        self.warn = wx.StaticText(self)
        top.Add(self.warn, 0, wx.LEFT | wx.RIGHT | wx.TOP, 14)

        # Two tabs keep the height fixed (a laptop screen): what to make, and the once-per-board settings.
        book = wx.Notebook(self)
        sb = wx.Panel(book)
        book.AddPage(sb, "Outputs")
        grid = wx.FlexGridSizer(2, 10, 18)
        self.ticks = {}
        for folder, keys in GROUPS:
            name = wx.StaticText(sb, label=folder.split("_", 1)[1])
            name.SetFont(name.GetFont().Bold())
            grid.Add(name, 0, wx.TOP, 1)
            cell = wx.GridSizer(3, 6, 18)
            for key in keys:
                self.ticks[key] = wx.CheckBox(sb, label=OUTPUTS[key])
                self.ticks[key].SetValue(cfg["outputs"][key])
                self.ticks[key].Bind(wx.EVT_CHECKBOX, self.refresh)
                cell.Add(self.ticks[key])
            if folder == FAB:  # the Gerber layers come from the board's File > Plot settings
                self.copper = grey(wx.StaticText(sb, label="00 copper layers"))  # set by read_board
                cell.Add(self.copper)
            grid.Add(cell)
        page = wx.BoxSizer(wx.VERTICAL)
        page.Add(grid, 0, wx.ALL, 12)
        sb.SetSizer(page)

        pane = wx.Panel(book)
        book.AddPage(pane, "BOM, name, prefab-gate")
        inner = wx.BoxSizer(wx.VERTICAL)
        form = wx.FlexGridSizer(2, 6, 8)
        form.AddGrowableCol(1)
        for key, label, tip in self.MORE:
            form.Add(wx.StaticText(pane, label=label), 0, wx.ALIGN_CENTER_VERTICAL)
            self.texts[key] = wx.TextCtrl(pane, value=cfg[key], size=(420, -1))
            self.texts[key].SetToolTip(tip)
            form.Add(self.texts[key], 1, wx.EXPAND)
        inner.Add(form, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 10)
        inner.Add(grey(wx.StaticText(pane, label="Hover a box for what it takes.")), 0, wx.LEFT | wx.TOP, 10)
        gbox = wx.StaticBoxSizer(wx.VERTICAL, pane, "prefab-gate")
        self.gate = {}
        for key, label in self.GATE:
            self.gate[key] = wx.CheckBox(gbox.GetStaticBox(), label=label)
            self.gate[key].SetValue(cfg["gate"][key])
            gbox.Add(self.gate[key], 0, wx.ALL, 3)
        gbox.Add(grey(wx.StaticText(gbox.GetStaticBox(), label=(
            "Which DRC errors block: Board Setup > Violation Severity, or Exclude in the DRC dialog."))),
            0, wx.ALL, 3)
        inner.Add(gbox, 0, wx.EXPAND | wx.ALL, 10)
        pane.SetSizer(inner)
        top.Add(book, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 14)
        for key in ("folder", "name", "bom_preset"):
            self.texts[key].Bind(wx.EVT_TEXT, self.refresh)

        note = grey(wx.StaticText(self, label=(
            f"Uses the board as saved on disk. Settings are kept in {self.board}.release.json; "
            "commit it to share them.")))
        note.Wrap(self.WRAP)
        top.Add(note, 0, wx.LEFT | wx.RIGHT | wx.TOP, 14)
        # The running release: hidden until Release is pressed.
        self.gauge = wx.Gauge(self, style=wx.GA_HORIZONTAL | wx.GA_SMOOTH)
        self.status = wx.StaticText(self)
        top.Add(self.gauge, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 14)
        top.Add(self.status, 0, wx.LEFT | wx.RIGHT | wx.TOP, 14)
        self.gauge.Hide()
        self.status.Hide()
        reset = wx.Button(self, label="Defaults")
        reset.Bind(wx.EVT_BUTTON, self.on_defaults)
        buttons = self.CreateStdDialogButtonSizer(wx.OK | wx.CANCEL)
        self.release_button = self.FindWindowById(wx.ID_OK)
        self.release_button.SetLabel("Release")
        self.release_button.Bind(wx.EVT_BUTTON, self.on_release)
        self.cancel_button = self.FindWindowById(wx.ID_CANCEL)
        self.cancel_button.Bind(wx.EVT_BUTTON, self.on_cancel)
        self.Bind(wx.EVT_CLOSE, self.on_cancel)
        row = wx.BoxSizer(wx.HORIZONTAL)
        row.Add(reset)
        if theme:
            pick = wx.Choice(self, choices=THEMES)
            pick.SetStringSelection(theme)
            pick.Bind(wx.EVT_CHOICE, self.on_theme)
            row.Add(wx.StaticText(self, label="Theme"), 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 14)
            row.Add(pick, 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 6)
        row.AddStretchSpacer()
        row.Add(buttons)
        top.Add(row, 0, wx.EXPAND | wx.ALL, 14)
        self.SetSizer(top)
        self.read_board()
        self.CentreOnScreen()
        # Coming back from KiCad (rev, text variables or Plot layers fixed and saved): re-read.
        self.Bind(wx.EVT_ACTIVATE, lambda e: (e.GetActive() and not self.busy and self.read_board(), e.Skip()))

    def on_theme(self, e):
        """wxMSW cannot change the appearance of open windows: main() saves it and reopens."""
        if not self.busy:
            self.theme = e.GetString()
            self.EndModal(wx.ID_RETRY)

    def read_board(self):
        """Rev, Gerber layers and git HEAD from the board as saved now, then refresh."""
        text = self.path.read_text(encoding="utf-8-sig")
        try:
            (rev, self.slug), self.rev_error = board_rev(self.path, text), None
        except Stop as e:
            rev, self.slug, self.rev_error = None, None, str(e)
        r = git(self.path.parent, "rev-parse", "HEAD")
        self.git_head = r.stdout.strip() if r.returncode == 0 else None
        self.head.SetLabel(f"{self.board}    rev {self.slug}" if rev else self.board)
        self.unplotted, copper = unplotted_layers(text), copper_layers(text)
        self.copper.SetLabel(f"{len(copper)} copper layers")
        self.copper.SetToolTip(f"{', '.join(copper)}\nLayers are chosen in File > Plot. Every release "
                               "checks the Gerbers hold all copper layers, F/B.Mask, F/B.SilkS, "
                               "Edge.Cuts and the drill.")
        self.refresh()

    def refresh(self, _=None):
        """Release name, problems and enabled boxes, from what is typed and ticked right now.
        The name, refusals and heads-ups come from release.py's own release_target and heads_up:
        red stops Release, amber only says what it will do."""
        preset = bool(self.texts["bom_preset"].GetValue().strip())
        for key in ("bom_fields", "bom_labels", "bom_group_by"):
            self.texts[key].Enable(not preset)
        problems, target, blocked = [], "", True
        try:
            if self.rev_error:
                raise Stop(4, self.rev_error)
            name, dest = release_target(self.path, self.config(), self.slug, datetime.date.today())
            shown = dest.relative_to(self.path.parent) if dest.is_relative_to(self.path.parent) else dest
            target = f"Will create   {shown}{os.sep}   and its .zip"
            problems += heads_up(self.path.parent, dest, tag_name(self.board, self.slug), self.git_head)
            blocked = False
        except Stop as e:
            problems.append(str(e))
        gerber = self.ticks["gerber"].GetValue()
        if gerber and self.unplotted:  # a warning only: the release's own Gerber check decides
            problems.append(f"Not ticked in File > Plot: {', '.join(self.unplotted)}. Tick them and "
                            "save the board, or the release stops at the Gerber check.")
        self.copper.Show(gerber)
        # long paths have no spaces: textwrap breaks them too, so the dialog never outgrows the screen
        self.target.SetLabel(textwrap.fill(target, 100, subsequent_indent="    "))
        self.target.Show(bool(target))
        self.warn.SetLabel("\n".join(textwrap.fill(f"⚠ {p}", 95, subsequent_indent="    ")
                                     for p in problems))
        self.warn.SetForegroundColour(wx.Colour(200, 0, 0) if blocked else wx.Colour(190, 100, 0))
        self.warn.Show(bool(problems))
        self.release_button.Enable(not blocked)
        # Fit only when lines appear or go: a Fit per keystroke would undo the user's resize.
        shape = (self.target.GetLabel().count("\n"), bool(target), self.warn.GetLabel().count("\n"),
                 bool(problems), gerber)
        if shape != self.shape:
            self.shape = shape
            self.Fit()
        else:
            self.Layout()

    def on_release(self, _):
        cfg = self.config()
        save_config(self.path.parent, self.board, cfg)
        self.busy, self.started = True, time.monotonic()
        for w in self.GetChildren():  # only Cancel stays live while it runs
            w.Enable(w in (self.cancel_button, self.gauge, self.status))
        self.gauge.Show()
        self.status.Show()
        self.Fit()
        try:
            self.code = release(self.path, cfg, self.progress)
        finally:
            self.busy = False
        self.EndModal(wx.ID_OK)

    def progress(self, done, total, msg):
        self.gauge.SetRange(total)
        self.gauge.SetValue(done)
        self.status.SetLabel(f"{msg}    {int(time.monotonic() - self.started)} s")
        wx.Yield()  # ponytail: polling loop on the UI thread; fine for one button and a gauge
        return not self.cancelled

    def on_cancel(self, e):
        if self.busy:
            self.cancelled = True
            self.status.SetLabel("Cancelling...")
        else:
            self.EndModal(wx.ID_CANCEL)

    def on_browse(self, _):
        with wx.DirDialog(self, "Release folder", str(self.path.parent / self.texts["folder"].GetValue())) as d:
            if d.ShowModal() == wx.ID_OK:
                self.texts["folder"].SetValue(d.GetPath())

    def on_defaults(self, _):
        for key, cb in self.ticks.items():
            cb.SetValue(DEFAULTS["outputs"][key])
        for key, tc in self.texts.items():
            tc.ChangeValue(DEFAULTS[key])
        for key, cb in self.gate.items():
            cb.SetValue(DEFAULTS["gate"][key])
        self.refresh()

    def config(self):
        return {"outputs": {k: cb.GetValue() for k, cb in self.ticks.items()},
                **{k: tc.GetValue().strip() for k, tc in self.texts.items()},
                "gate": {k: cb.GetValue() for k, cb in self.gate.items()}}


if __name__ == "__main__":
    sys.exit(main())
