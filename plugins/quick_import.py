"""
Quick Part Import - drop part ZIPs, get them in your KiCad project's own library.

Started from the toolbar button in the schematic or PCB editor (KiCad IPC plugin).
Clicking the button again just brings the existing window to the front.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import queue
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))



def _data_dir() -> Path:
    """KiCad's settings folder for this plugin, so settings survive plugin updates."""
    try:
        import project_detect

        plugin_id = json.loads((HERE / "plugin.json").read_text(encoding="utf-8"))["identifier"]
        config = project_detect.kicad_config_dir()
        if config:
            folder = config / "plugins" / plugin_id
            folder.mkdir(parents=True, exist_ok=True)
            return folder
    except (OSError, ValueError, KeyError):
        pass
    return HERE


DATA_DIR = _data_dir()
LOG_FILE = DATA_DIR / "quick_part_import.log"
SETTINGS_FILE = DATA_DIR / "settings.json"
PORT =50000 + int(hashlib.md5(str(HERE).lower().encode()).hexdigest(), 16) % 10000
log = logging.getLogger("quick_part_import")


# ----------------------------------------------------------------------------
# Single instance: a second launch only asks the running window to come forward
# ----------------------------------------------------------------------------


def _launch_message() -> bytes:
    return (
        json.dumps(
            {
                "cmd": "raise",
                "socket": os.environ.get("KICAD_API_SOCKET"),
                "token": os.environ.get("KICAD_API_TOKEN"),
            }
        ).encode()
        + b"\n"
    )


def notify_running_instance() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", PORT), timeout=0.5) as conn:
            conn.sendall(_launch_message())
            conn.settimeout(2)
            return conn.recv(8).startswith(b"ok")
    except OSError:
        return False


class InstanceServer(threading.Thread):
    def __init__(self, on_message) -> None:
        super().__init__(daemon=True)
        self.on_message = on_message
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        self.sock.bind(("127.0.0.1", PORT))
        self.sock.listen(4)

    def run(self) -> None:
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            with conn:
                try:
                    conn.settimeout(2)
                    data = conn.recv(4096).decode("utf-8", errors="replace").strip()
                    self.on_message(json.loads(data) if data else {})
                    conn.sendall(b"ok\n")
                except (OSError, ValueError) as e:
                    log.debug("Bad instance message: %s", e)

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------


def load_settings() -> dict:
    try:
        return json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_settings(settings: dict) -> None:
    try:
        SETTINGS_FILE.write_text(json.dumps(settings, indent=2), encoding="utf-8")
    except OSError as e:
        log.warning("Could not save settings: %s", e)


def downloads_dir() -> Path:
    if sys.platform == "win32":
        try:
            import ctypes
            import uuid
            from ctypes import wintypes

            class GUID(ctypes.Structure):
                _fields_ = [
                    ("Data1", wintypes.DWORD),
                    ("Data2", wintypes.WORD),
                    ("Data3", wintypes.WORD),
                    ("Data4", ctypes.c_ubyte * 8),
                ]

            guid = GUID()
            folder_id = uuid.UUID("{374DE290-123F-4565-9164-39C4925E467B}")  # FOLDERID_Downloads
            ctypes.memmove(ctypes.byref(guid), folder_id.bytes_le, 16)
            path_ptr = ctypes.c_wchar_p()
            if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(guid), 0, None, ctypes.byref(path_ptr)) == 0:
                path = path_ptr.value
                ctypes.windll.ole32.CoTaskMemFree(path_ptr)
                if path:
                    return Path(path)
        except Exception as e:
            log.debug("Known folder lookup failed: %s", e)
    return Path.home() / "Downloads"


def open_folder(path: Path) -> None:
    if sys.platform == "win32":
        os.startfile(str(path))  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


class DownloadWatcher(threading.Thread):
    """Reports part ZIPs that newly appear (fully written) in a folder."""

    def __init__(self, folder: Path, on_new) -> None:
        super().__init__(daemon=True)
        self.folder = folder
        self.on_new = on_new
        self.stop_event = threading.Event()

    def _scan(self) -> dict[Path, tuple[int, int]]:
        found = {}
        try:
            for path in self.folder.iterdir():
                if path.suffix.lower() == ".zip":
                    try:
                        stat = path.stat()
                        found[path] = (stat.st_size, stat.st_mtime_ns)
                    except OSError:
                        pass
        except OSError:
            pass
        return found

    def run(self) -> None:
        import part_importer as pi

        seen = self._scan()  # files that were already there are ignored
        pending: dict[Path, tuple[int, int]] = {}
        while not self.stop_event.wait(1.5):
            for path, signature in self._scan().items():
                if seen.get(path) == signature:
                    continue
                if pending.get(path) != signature:
                    pending[path] = signature  # wait until the size stops changing
                    continue
                pending.pop(path, None)
                seen[path] = signature
                if signature[0] > 0 and pi.looks_like_part_zip(path):
                    self.on_new(path)
                else:
                    log.info("Ignored download %s (no KiCad files)", path.name)

    def stop(self) -> None:
        self.stop_event.set()


# ----------------------------------------------------------------------------
# GUI
# ----------------------------------------------------------------------------


def run_gui(args: list[str]) -> int:
    import wx

    import part_importer as pi
    import project_detect

    class FileDrop(wx.FileDropTarget):
        def __init__(self, frame: "MainFrame") -> None:
            super().__init__()
            self.frame = frame

        def OnEnter(self, x, y, default):
            self.frame.zone.set_hover(True)
            return wx.DragCopy

        def OnDragOver(self, x, y, default):
            return wx.DragCopy

        def OnLeave(self):
            self.frame.zone.set_hover(False)

        def OnDropFiles(self, x, y, filenames):
            self.frame.zone.set_hover(False)
            wx.CallAfter(self.frame.import_files, list(filenames))
            return True

    class DropZone(wx.Panel):
        def __init__(self, parent: wx.Window, on_click) -> None:
            super().__init__(parent, style=wx.FULL_REPAINT_ON_RESIZE)
            self.SetBackgroundStyle(wx.BG_STYLE_PAINT)
            self.on_click = on_click
            self.hover = False
            self.busy = 0
            self.has_project = True
            self.SetMinSize(self.FromDIP(wx.Size(-1, 190)))
            self.SetCursor(wx.Cursor(wx.CURSOR_HAND))
            self.SetToolTip("Drop ZIP files here, or click to choose them")
            self.Bind(wx.EVT_PAINT, self._paint)
            self.Bind(wx.EVT_LEFT_UP, lambda e: self.on_click())
            self.Bind(wx.EVT_ENTER_WINDOW, lambda e: self._mouse(True))
            self.Bind(wx.EVT_LEAVE_WINDOW, lambda e: self._mouse(False))
            self.mouse_inside = False

        def _mouse(self, inside: bool) -> None:
            self.mouse_inside = inside
            self.Refresh()

        def set_hover(self, hover: bool) -> None:
            self.hover = hover
            self.Refresh()

        def set_busy(self, busy: int) -> None:
            self.busy = busy
            self.Refresh()

        @staticmethod
        def _mix(a: wx.Colour, b: wx.Colour, t: float) -> wx.Colour:
            return wx.Colour(*(int(x + (y - x) * t) for x, y in zip(a.Get()[:3], b.Get()[:3])))

        def _paint(self, _event) -> None:
            dc = wx.AutoBufferedPaintDC(self)
            bg = self.GetParent().GetBackgroundColour()
            dc.SetBackground(wx.Brush(bg))
            dc.Clear()
            gc = wx.GraphicsContext.Create(dc)
            if gc is None:
                return
            fg = wx.SystemSettings.GetColour(wx.SYS_COLOUR_WINDOWTEXT)
            accent = wx.Colour(0, 120, 212)
            dark = bg.GetLuminance() < 0.5
            if dark:
                accent = wx.Colour(96, 170, 255)
            active = self.hover or self.mouse_inside
            fill = self._mix(bg, accent, 0.18 if self.hover else (0.08 if active else 0.04))
            border = accent if active else self._mix(bg, fg, 0.35)

            w, h = self.GetClientSize()
            m = self.FromDIP(6)
            gc.SetBrush(wx.Brush(fill))
            gc.SetPen(wx.Pen(border, self.FromDIP(2), wx.PENSTYLE_SHORT_DASH))
            gc.DrawRoundedRectangle(m, m, w - 2 * m, h - 2 * m, self.FromDIP(12))

            # down arrow into a tray
            cx, top = w / 2, h / 2 - self.FromDIP(62)
            s = self.FromDIP(1)
            gc.SetPen(wx.Pen(accent if active else self._mix(bg, fg, 0.55), self.FromDIP(3)))
            gc.StrokeLine(cx, top, cx, top + 26 * s)
            gc.StrokeLines([(cx - 10 * s, top + 16 * s), (cx, top + 26 * s), (cx + 10 * s, top + 16 * s)])
            gc.StrokeLines([(cx - 18 * s, top + 24 * s), (cx - 18 * s, top + 34 * s), (cx + 18 * s, top + 34 * s), (cx + 18 * s, top + 24 * s)])

            if self.busy:
                title, sub = "Importing…", f"{self.busy} file(s) in progress"
            elif not self.has_project:
                title, sub = "Choose a project first", "click here or use “Change…” above"
            elif self.hover:
                title, sub = "Release to import", ""
            else:
                title, sub = "Drop part ZIPs here", "Component Search Engine · SnapEDA · UltraLibrarian · Octopart"
            big = self.GetFont().Bold()
            big.SetPointSize(big.GetPointSize() + 5)
            gc.SetFont(big, fg)
            tw, th = gc.GetTextExtent(title)
            y = top + 46 * s
            gc.DrawText(title, (w - tw) / 2, y)
            y += th + self.FromDIP(6)
            small = self.GetFont()
            muted = self._mix(bg, fg, 0.6)
            gc.SetFont(small, muted)
            for line in (sub, "" if self.busy or self.hover else "or click to choose files"):
                if line:
                    lw, lh = gc.GetTextExtent(line)
                    gc.DrawText(line, (w - lw) / 2, y)
                    y += lh + self.FromDIP(3)

    class MainFrame(wx.Frame):
        def __init__(self, project_hint: Path | None) -> None:
            super().__init__(None, title="Quick Part Import", size=wx.Size(600, 640))
            self.SetSize(self.FromDIP(wx.Size(600, 640)))
            self.SetMinSize(self.FromDIP(wx.Size(440, 480)))
            icon = HERE / "icon-48.png"
            if icon.exists():
                self.SetIcon(wx.Icon(str(icon), wx.BITMAP_TYPE_PNG))

            self.settings = load_settings()
            self.cli_path = pi.find_kicad_cli()
            self.api_socket = os.environ.get("KICAD_API_SOCKET")
            self.api_token = os.environ.get("KICAD_API_TOKEN")
            self.project: Path | None = None
            self.project_manual = False
            self.last_detect = 0.0
            self.pending = 0
            self.jobs: queue.Queue = queue.Queue()
            self.watcher: DownloadWatcher | None = None
            self.server: InstanceServer | None = None
            self.closing = False

            self._build()
            self.worker = threading.Thread(target=self._work, daemon=True)
            self.worker.start()

            if project_hint:
                self.set_project(project_hint, manual=True)
            else:
                last = self.settings.get("last_project")
                if last and Path(last).is_file():
                    self.set_project(Path(last), manual=False, remember=False)
                self.detect_project()
            if self.settings.get("watch_downloads"):
                self.watch_box.SetValue(True)
                self.toggle_watch()
            if self.settings.get("stay_on_top"):
                self.top_box.SetValue(True)
                self.toggle_on_top()
            if not self.cli_path:
                self.log_line("⚠", "kicad-cli was not found – only new-format files can be imported.", "warn")

            self.Bind(wx.EVT_CLOSE, self.on_close)
            self.Bind(wx.EVT_ACTIVATE, self.on_activate)

        # -- layout ---------------------------------------------------------------

        def _build(self) -> None:
            panel = wx.Panel(self)
            self.panel = panel
            pad = self.FromDIP(12)
            root = wx.BoxSizer(wx.VERTICAL)

            head = wx.BoxSizer(wx.HORIZONTAL)
            label = wx.StaticText(panel, label="Project:")
            self.project_name = wx.StaticText(panel, label="…")
            self.project_name.SetFont(self.project_name.GetFont().Bold().Scaled(1.15))
            change = wx.Button(panel, label="Change…")
            change.SetToolTip("Choose the .kicad_pro file of the project to import into")
            change.Bind(wx.EVT_BUTTON, lambda e: self.choose_project())
            head.Add(label, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, self.FromDIP(6))
            head.Add(self.project_name, 1, wx.ALIGN_CENTER_VERTICAL)
            head.Add(change, 0, wx.ALIGN_CENTER_VERTICAL)
            root.Add(head, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, pad)

            self.project_path = wx.StaticText(panel, label="", style=wx.ST_ELLIPSIZE_MIDDLE)
            self.project_path.SetForegroundColour(wx.SystemSettings.GetColour(wx.SYS_COLOUR_GRAYTEXT))
            root.AddSpacer(self.FromDIP(4))
            root.Add(self.project_path, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, pad)
            root.AddSpacer(self.FromDIP(8))

            self.zone = DropZone(panel, self.on_zone_click)
            root.Add(self.zone, 3, wx.EXPAND | wx.LEFT | wx.RIGHT, pad)

            self.log = wx.TextCtrl(
                panel, style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_RICH2 | wx.TE_NOHIDESEL
            )
            self.log.SetMinSize(self.FromDIP(wx.Size(-1, 130)))
            root.Add(self.log, 2, wx.EXPAND | wx.ALL, pad)

            self.watch_folder = Path(self.settings.get("downloads_folder") or downloads_dir())
            self.watch_box = wx.CheckBox(panel, label=f"Auto-import part ZIPs that land in {self.watch_folder.name}")
            self.watch_box.SetToolTip(f"Watches {self.watch_folder} while this window is open")
            self.watch_box.Bind(wx.EVT_CHECKBOX, lambda e: self.toggle_watch())
            self.top_box = wx.CheckBox(panel, label="Keep this window on top")
            self.top_box.Bind(wx.EVT_CHECKBOX, lambda e: self.toggle_on_top())
            options = wx.BoxSizer(wx.VERTICAL)
            options.Add(self.watch_box, 0)
            options.Add(self.top_box, 0, wx.TOP, self.FromDIP(4))
            root.Add(options, 0, wx.LEFT | wx.RIGHT, pad)

            foot = wx.BoxSizer(wx.HORIZONTAL)
            open_btn = wx.Button(panel, label="Open library folder")
            open_btn.Bind(wx.EVT_BUTTON, lambda e: self.open_library())
            close_btn = wx.Button(panel, wx.ID_CLOSE, label="Close")
            close_btn.Bind(wx.EVT_BUTTON, lambda e: self.Close())
            foot.Add(open_btn, 0)
            foot.AddStretchSpacer()
            foot.Add(close_btn, 0)
            root.Add(foot, 0, wx.EXPAND | wx.ALL, pad)

            panel.SetSizer(root)
            for window in (panel, self.zone, self.log, self.project_name, self.project_path):
                window.SetDropTarget(FileDrop(self))

        # -- logging into the window ------------------------------------------------

        def log_line(self, mark: str, text: str, kind: str = "info") -> None:
            colours = {
                "ok": wx.Colour(16, 124, 16),
                "warn": wx.Colour(196, 110, 0),
                "error": wx.Colour(200, 30, 30),
                "note": wx.Colour(0, 102, 204),
                "info": wx.SystemSettings.GetColour(wx.SYS_COLOUR_GRAYTEXT),
            }
            if self.log.GetBackgroundColour().GetLuminance() < 0.5:
                colours.update(ok=wx.Colour(110, 200, 110), error=wx.Colour(255, 110, 110), note=wx.Colour(110, 180, 255))
            self.log.SetDefaultStyle(wx.TextAttr(colours.get(kind, colours["info"])))
            prefix = "" if self.log.IsEmpty() else "\n"
            self.log.AppendText(f"{prefix}{mark}  {text}" if mark else f"{prefix}{text}")
            self.log.ShowPosition(self.log.GetLastPosition())

        # -- project -------------------------------------------------------------------

        def set_project(self, project: Path | None, manual: bool, remember: bool = True) -> None:
            if project is not None:
                project = Path(project)
            changed = project != self.project
            self.project = project
            self.project_manual = manual
            self.zone.has_project = project is not None
            self.zone.Refresh()
            if project is None:
                self.project_name.SetLabel("none detected")
                self.project_path.SetLabel("Open a project in KiCad or click “Change…”")
            else:
                library = pi.ProjectLibrary(project, pi.KicadCli(None))
                self.project_name.SetLabel(project.stem)
                rel = library.sym_file.relative_to(project.parent)
                self.project_path.SetLabel(f"{project.parent}   →   {rel}")
                self.project_path.SetToolTip(f"Parts are stored in {library.lib_dir}")
                if remember:
                    self.settings["last_project"] = str(project)
                    save_settings(self.settings)
                if changed:
                    log.info("Project: %s (%s)", project, "manual" if manual else "detected")
            self.panel.Layout()

        def detect_project(self) -> None:
            self.last_detect = time.monotonic()
            sock, token = self.api_socket, self.api_token

            def work() -> None:
                try:
                    found = project_detect.detect_project(sock, token)
                except Exception:
                    log.exception("Project detection failed")
                    found = None
                wx.CallAfter(self._detected, found)

            threading.Thread(target=work, daemon=True).start()

        def _detected(self, found: Path | None) -> None:
            if self.closing or self.project_manual:
                return
            if found is not None:
                if found != self.project:
                    self.set_project(found, manual=False)
            elif self.project is None:
                self.set_project(None, manual=False)

        def choose_project(self) -> bool:
            start = str(self.project.parent) if self.project else ""
            with wx.FileDialog(
                self,
                "Choose the KiCad project to import into",
                defaultDir=start,
                wildcard="KiCad project (*.kicad_pro)|*.kicad_pro",
                style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST,
            ) as dlg:
                if dlg.ShowModal() != wx.ID_OK:
                    return False
                self.set_project(Path(dlg.GetPath()), manual=True)
                return True

        def on_activate(self, event: wx.ActivateEvent) -> None:
            if event.GetActive() and not self.project_manual and time.monotonic() - self.last_detect > 3:
                self.detect_project()
            event.Skip()

        def on_remote(self, message: dict) -> None:
            """Another launch from KiCad: come to the front and follow that KiCad window."""
            if message.get("socket"):
                self.api_socket = message.get("socket")
                self.api_token = message.get("token")
            self.project_manual = False
            self.detect_project()
            if self.IsIconized():
                self.Iconize(False)
            self.Show()
            self.Raise()
            self.RequestUserAttention()

        # -- importing -----------------------------------------------------------------

        def on_zone_click(self) -> None:
            if self.project is None and not self.choose_project():
                return
            with wx.FileDialog(
                self,
                "Choose part ZIP files",
                defaultDir=str(downloads_dir()),
                wildcard="Part libraries (*.zip)|*.zip|KiCad files (*.kicad_sym;*.kicad_mod;*.lib;*.step;*.stp)|*.kicad_sym;*.kicad_mod;*.lib;*.step;*.stp|All files (*.*)|*.*",
                style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST | wx.FD_MULTIPLE,
            ) as dlg:
                if dlg.ShowModal() == wx.ID_OK:
                    self.import_files(dlg.GetPaths())

        def import_files(self, names: list[str]) -> None:
            if self.project is None and not self.choose_project():
                return
            paths = [Path(n) for n in names]
            zips = [p for p in paths if p.suffix.lower() == ".zip" or p.is_dir()]
            loose = [p for p in paths if p not in zips]
            groups = [([z], z.name) for z in zips]
            if loose:
                label = ", ".join(p.name for p in loose[:3]) + (" …" if len(loose) > 3 else "")
                groups.append((loose, label))
            for group, label in groups:
                self.pending += 1
                self.jobs.put((self.project, group, label))
            self.zone.set_busy(self.pending)

        def import_download(self, path: Path) -> None:
            if self.closing:
                return
            if self.project is None:
                self.log_line("⚠", f"New download {path.name} skipped – no project selected.", "warn")
                return
            self.log_line("⬇", f"New download: {path.name}", "info")
            self.import_files([str(path)])

        def _work(self) -> None:
            while True:
                job = self.jobs.get()
                if job is None:
                    return
                project, paths, label = job
                try:
                    result = pi.import_into_project(project, paths, self.cli_path, label)
                    outcome = (result, None)
                except pi.PartImportError as e:
                    log.warning("Import of %s failed: %s", label, e)
                    outcome = (None, str(e))
                except Exception as e:
                    log.exception("Unexpected error while importing %s", label)
                    outcome = (None, f"Unexpected error: {e}  (details: {LOG_FILE})")
                wx.CallAfter(self._finished, label, project, *outcome)

        def _finished(self, label: str, project: Path, result, error: str | None) -> None:
            if self.closing:
                return
            self.pending = max(0, self.pending - 1)
            self.zone.set_busy(self.pending)
            if error:
                self.log_line("✖", f"{label}: {error}", "error")
                return
            self.log_line("✔", f"{result.part} — {result.summary()}", "ok")
            for symbol, footprint in result.links[:3]:
                self.log_line("", f"      {result.library}:{symbol}  →  footprint {footprint}", "info")
            if result.symbols and not result.links:
                self.log_line("", f"      {result.library}:{result.symbols[0]}  (no footprint in this download)", "info")
            for warning in result.warnings:
                self.log_line("⚠", warning, "warn")
            if result.library_added:
                self.log_line(
                    "ℹ",
                    f"Library “{result.library}” was added to {project.stem}. If it does not show up in "
                    "Add Symbol yet, close and reopen the project once (only needed the first time).",
                    "note",
                )

        # -- options -------------------------------------------------------------------

        def toggle_watch(self) -> None:
            on = self.watch_box.GetValue()
            if self.watcher:
                self.watcher.stop()
                self.watcher = None
            if on:
                folder = self.watch_folder
                self.watcher = DownloadWatcher(folder, lambda p: wx.CallAfter(self.import_download, p))
                self.watcher.start()
                self.log_line("", f"Watching {folder} for new part ZIPs.", "info")
            self.settings["watch_downloads"] = on
            save_settings(self.settings)

        def toggle_on_top(self) -> None:
            on = self.top_box.GetValue()
            style = self.GetWindowStyleFlag()
            self.SetWindowStyleFlag(style | wx.STAY_ON_TOP if on else style & ~wx.STAY_ON_TOP)
            self.settings["stay_on_top"] = on
            save_settings(self.settings)

        def open_library(self) -> None:
            if self.project is None:
                self.choose_project()
                return
            library = pi.ProjectLibrary(self.project, pi.KicadCli(None))
            open_folder(library.lib_dir if library.lib_dir.is_dir() else self.project.parent)

        def on_close(self, event: wx.CloseEvent) -> None:
            if self.pending and event.CanVeto():
                answer = wx.MessageBox(
                    "An import is still running. Close anyway?",
                    "Quick Part Import",
                    wx.YES_NO | wx.ICON_QUESTION,
                    self,
                )
                if answer != wx.YES:
                    event.Veto()
                    return
            self.closing = True
            if self.watcher:
                self.watcher.stop()
            if self.server:
                self.server.close()
            self.jobs.put(None)
            event.Skip()

    # -- start ---------------------------------------------------------------------

    project_hint = None
    if "--project" in args:
        project_hint = Path(args[args.index("--project") + 1])

    app = wx.App(False)
    if hasattr(app, "MSWEnableDarkMode"):
        try:
            app.MSWEnableDarkMode()
        except Exception:
            pass
    frame = MainFrame(project_hint)
    try:
        frame.server = InstanceServer(lambda msg: wx.CallAfter(frame.on_remote, msg))
        frame.server.start()
    except OSError as e:
        log.info("Single-instance server not started: %s", e)
    frame.Show()
    frame.Raise()

    if "--selftest" in args:  # used for testing: drop files, screenshot, quit
        drops = [d for d in args[args.index("--selftest") + 1].split(";") if d]
        shot = args[args.index("--screenshot") + 1] if "--screenshot" in args else None
        close_after = float(args[args.index("--close-after") + 1]) if "--close-after" in args else 2.5
        started = time.monotonic()

        def finish() -> None:
            if frame.pending or time.monotonic() - started < close_after:
                wx.CallLater(300, finish)
                return
            if shot:
                frame.Raise()
                frame.Update()
                rect = frame.GetScreenRect()
                dc = wx.ScreenDC()
                bmp = wx.Bitmap(rect.width, rect.height, 24)
                mem = wx.MemoryDC(bmp)
                mem.Blit(0, 0, rect.width, rect.height, dc, rect.x, rect.y)
                mem.SelectObject(wx.NullBitmap)
                bmp.SaveFile(shot, wx.BITMAP_TYPE_PNG)
            log.info("SELFTEST LOG:\n%s", frame.log.GetValue())
            frame.Close(force=True)

        if drops:
            wx.CallLater(1500, lambda: frame.import_files(drops))
        wx.CallLater(2500, finish)

    app.MainLoop()
    return 0


def main() -> int:
    if "--selftest" not in sys.argv and notify_running_instance():
        return 0  # the open window was brought to the front
    logging.basicConfig(
        filename=LOG_FILE,
        filemode="w",
        level=logging.DEBUG,
        format="%(asctime)s %(levelname)s %(message)s",
        encoding="utf-8",
    )
    log.info("Python %s, args %s", sys.version.split()[0], sys.argv[1:])
    sys.excepthook = lambda *exc: log.critical("Uncaught error", exc_info=exc)
    try:
        return run_gui(sys.argv[1:])
    except Exception as e:
        log.exception("Quick Part Import crashed")
        try:
            import wx

            _app = wx.App(False) if not wx.GetApp() else None
            wx.MessageBox(f"Quick Part Import could not start:\n\n{e}\n\nDetails: {LOG_FILE}", "Quick Part Import", wx.ICON_ERROR)
        except Exception:
            pass
        return 1


if __name__ == "__main__":
    sys.exit(main())
