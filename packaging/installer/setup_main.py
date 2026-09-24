"""MisterySetup.exe — the window, and the command line behind it.

tkinter, not PySide6. The app needs Qt; an installer does not. Measured: the
frozen installer is 11.2 MB before the app payload is glued onto it, and Qt
would have added tens of megabytes to draw four checkboxes and a progress bar.

    MisterySetup.exe and Uninstall.exe are the same 11.2 MB program. Which one
    it is, it decides from its own filename (runs_as_uninstaller), because
    Windows runs the UninstallString from Add/Remove Programs with no arguments
    at all.

Everything this window does, it does by calling install_steps.run() or
uninstall_steps.run() on a worker thread and drawing what they report. It
contains no knowledge of folders, registry keys or task names, which is what
lets --unattended run exactly the same install without a screen attached.

    MisterySetup.exe                     the window
    MisterySetup.exe --unattended        the same install, printing as it goes
    MisterySetup.exe --uninstall         the remove window
    Uninstall.exe --quiet                remove, no questions, keeps the library
"""

from __future__ import annotations

import argparse
import os
import queue
import sys
import threading
import time
from pathlib import Path

from . import install_steps, payload, uninstall_steps
from .setup_common import (APP_NAME, Cancelled, InstallError,
                           default_install_dir, human_size, is_frozen)

WINDOW_BACKGROUND = "#ffffff"
MUTED = "#5b5f66"


# --- the window -------------------------------------------------------------


def _icon_path() -> Path | None:
    """assets/icon.ico, unpacked beside the installer's own Python payload."""
    base = getattr(sys, "_MEIPASS", None)
    if base:
        candidate = Path(base) / "icon.ico"
        return candidate if candidate.is_file() else None
    here = Path(__file__).resolve().parent.parent.parent / "assets" / "icon.ico"
    return here if here.is_file() else None


class Window:
    """One window that installs, and the same window that uninstalls.

    The two flows share the frame, the progress bar and the Cancel handling
    because they are the same three things happening in a different order, and
    keeping them in one class is how the Remove window inherited the install
    window's habit of never lying about progress.
    """

    def __init__(self, title: str, subtitle: str) -> None:
        import tkinter as tk
        from tkinter import ttk

        self.tk = tk
        self.ttk = ttk
        self.root = tk.Tk()
        self.root.title(title)
        self.root.configure(background=WINDOW_BACKGROUND)
        self.root.resizable(False, False)
        icon = _icon_path()
        if icon is not None:
            try:
                self.root.iconbitmap(str(icon))
            except tk.TclError:
                pass                 # a missing icon is not worth failing over

        style = ttk.Style(self.root)
        try:
            style.theme_use("vista")
        except tk.TclError:
            pass
        style.configure("TFrame", background=WINDOW_BACKGROUND)
        style.configure("TLabel", background=WINDOW_BACKGROUND)
        style.configure("TCheckbutton", background=WINDOW_BACKGROUND)
        style.configure("Muted.TLabel", foreground=MUTED)
        style.configure("Title.TLabel", font=("Segoe UI Semibold", 16))
        style.configure("Sub.TLabel", foreground=MUTED)

        self.frame = ttk.Frame(self.root, padding=(22, 18, 22, 16))
        self.frame.grid(sticky="nsew")

        ttk.Label(self.frame, text=title, style="Title.TLabel").grid(
            row=0, column=0, columnspan=3, sticky="w")
        ttk.Label(self.frame, text=subtitle, style="Sub.TLabel",
                  wraplength=480, justify="left").grid(
            row=1, column=0, columnspan=3, sticky="w", pady=(2, 14))

        self.body = ttk.Frame(self.frame)
        self.body.grid(row=2, column=0, columnspan=3, sticky="ew")

        self.status = tk.StringVar(value="")
        self.detail = tk.StringVar(value="")
        self.bar = ttk.Progressbar(self.frame, length=480, mode="determinate",
                                   maximum=1000)
        self.bar.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(16, 6))
        ttk.Label(self.frame, textvariable=self.status).grid(
            row=4, column=0, columnspan=3, sticky="w")
        # wraplength, because the detail line carries install paths and the
        # "left behind" list, and a long path with no wrap stretches the window
        # sideways off the screen instead of taking a second line.
        ttk.Label(self.frame, textvariable=self.detail, style="Muted.TLabel",
                  wraplength=480, justify="left").grid(
            row=5, column=0, columnspan=3, sticky="w", pady=(0, 12))

        self.buttons = ttk.Frame(self.frame)
        self.buttons.grid(row=6, column=0, columnspan=3, sticky="e")

        self.messages: queue.Queue = queue.Queue()
        self.cancel = threading.Event()
        self.worker: threading.Thread | None = None
        self.finished = False
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # -- plumbing

    def report(self, what: str, fraction: float) -> None:
        """Called from the worker thread. Only ever puts things on a queue."""
        self.messages.put(("progress", what, fraction))

    def _pump(self) -> None:
        try:
            while True:
                kind, *rest = self.messages.get_nowait()
                if kind == "progress":
                    what, fraction = rest
                    self.status.set(what)
                    self.bar["value"] = int(fraction * 1000)
                elif kind == "detail":
                    self.detail.set(rest[0])
                elif kind == "done":
                    self.finished = True
                    self.on_done(*rest)
        except queue.Empty:
            pass
        self.root.after(70, self._pump)

    def start(self, work) -> None:
        """Run `work()` on a thread; it reports through self.report."""
        def wrapper() -> None:
            try:
                self.messages.put(("done", True, work()))
            except Cancelled:
                self.messages.put(("done", None, None))
            except InstallError as error:
                self.messages.put(("done", False, error))
            except Exception as error:          # noqa: BLE001 — shown, not swallowed
                self.messages.put(("done", False, error))

        self.worker = threading.Thread(target=wrapper, daemon=True)
        self.worker.start()

    def on_done(self, ok, payload_):            # overridden
        raise NotImplementedError

    def _on_close(self) -> None:
        if self.worker is not None and self.worker.is_alive() and not self.finished:
            self.cancel.set()
            self.status.set("Stopping…")
            return
        self.root.destroy()

    def run(self) -> None:
        self.root.after(70, self._pump)
        self.root.update_idletasks()
        # Centred on the screen rather than at Tk's top-left default, which on a
        # 4K monitor puts the window in a corner the person is not looking at.
        width, height = self.root.winfo_width(), self.root.winfo_height()
        x = (self.root.winfo_screenwidth() - width) // 2
        y = (self.root.winfo_screenheight() - height) // 3
        self.root.geometry(f"+{max(0, x)}+{max(0, y)}")
        self.root.mainloop()

    def show_error(self, error: Exception) -> None:
        from tkinter import messagebox

        self.bar["value"] = 0
        self.status.set("The install did not finish.")
        self.detail.set(str(error).splitlines()[0][:110])
        messagebox.showerror(f"{APP_NAME} Setup", str(error), parent=self.root)


class InstallWindow(Window):
    def __init__(self, options: install_steps.Options) -> None:
        version = payload.version() or "?"
        super().__init__(
            f"Install {APP_NAME} {version}",
            "Mistery is a player for the movies, shows and music already on "
            "this PC. Nothing is uploaded anywhere.")
        tk, ttk = self.tk, self.ttk
        self.options = options
        self.result: install_steps.Result | None = None

        ttk.Label(self.body, text="Install to").grid(row=0, column=0, sticky="w")
        self.folder = tk.StringVar(value=str(options.install_dir))
        entry = ttk.Entry(self.body, textvariable=self.folder, width=54)
        entry.grid(row=1, column=0, sticky="ew", pady=(2, 2))
        ttk.Button(self.body, text="Browse…", command=self._browse).grid(
            row=1, column=1, padx=(8, 0))
        self.body.columnconfigure(0, weight=1)

        app_bytes = payload.unpacked_size()
        from .tool_downloads import DOWNLOAD_BYTES, RUNTIME_BYTES, TOOLS
        # Says where mpv and ffmpeg come from and under what licence, before the
        # Install button rather than in a file nobody opens. They are fetched
        # from the projects that build them, which is the honest arrangement for
        # GPL and LGPL programs Mistery does not itself distribute.
        self.size_note = tk.StringVar(value=(
            f"About {human_size(app_bytes + RUNTIME_BYTES)} on disk: "
            f"{human_size(app_bytes)} of Mistery, plus {human_size(RUNTIME_BYTES)} "
            f"of mpv and ffmpeg ({human_size(DOWNLOAD_BYTES)} to download) "
            "fetched from the projects that build them — "
            + "; ".join(tool.license_note for tool in TOOLS) + "."))
        ttk.Label(self.body, textvariable=self.size_note, style="Muted.TLabel",
                  wraplength=470, justify="left").grid(
            row=2, column=0, columnspan=2, sticky="w", pady=(2, 12))

        self.start_menu = tk.BooleanVar(value=options.start_menu_shortcut)
        self.desktop = tk.BooleanVar(value=options.desktop_shortcut)
        self.updates = tk.BooleanVar(value=options.register_update_task)
        ttk.Checkbutton(self.body, text="Add to the Start Menu",
                        variable=self.start_menu).grid(row=3, column=0,
                                                       columnspan=2, sticky="w")
        ttk.Checkbutton(self.body, text="Put a shortcut on the Desktop",
                        variable=self.desktop).grid(row=4, column=0,
                                                    columnspan=2, sticky="w")
        ttk.Checkbutton(self.body,
                        text="Check for updates once an hour, and install them "
                             "while Mistery is closed",
                        variable=self.updates).grid(row=5, column=0,
                                                    columnspan=2, sticky="w")

        self.cancel_button = ttk.Button(self.buttons, text="Cancel",
                                        command=self._on_close)
        self.cancel_button.grid(row=0, column=0, padx=(0, 8))
        self.install_button = ttk.Button(self.buttons, text="Install",
                                         command=self._install)
        self.install_button.grid(row=0, column=1)
        self.status.set("Ready.")

    def _browse(self) -> None:
        from tkinter import filedialog

        chosen = filedialog.askdirectory(
            parent=self.root, title=f"Where should {APP_NAME} go?",
            initialdir=str(Path(self.folder.get()).parent))
        if chosen:
            # Chosen an empty folder that is not already ours: go in as-is.
            # Chosen a parent to put it under: add the name, the way every other
            # installer's Browse button behaves.
            path = Path(chosen)
            if path.name.lower() != APP_NAME.lower() and any(path.iterdir()):
                path = path / APP_NAME
            self.folder.set(str(path))

    def _install(self) -> None:
        folder = Path(self.folder.get().strip('" '))
        try:
            note = install_steps.check_target(folder)
        except InstallError as error:
            self.show_error(error)
            return
        if note:
            self.detail.set(note)

        self.options.install_dir = folder
        self.options.start_menu_shortcut = self.start_menu.get()
        self.options.desktop_shortcut = self.desktop.get()
        self.options.register_update_task = self.updates.get()
        for widget in self.body.winfo_children():
            try:
                widget.state(["disabled"])
            except (AttributeError, self.tk.TclError):
                pass
        self.install_button.state(["disabled"])
        self.start(lambda: install_steps.run(self.options, self.report, self.cancel))

    def on_done(self, ok, value) -> None:
        if ok is None:
            self.status.set("Cancelled. Nothing was left behind.")
            self.detail.set("")
            self.bar["value"] = 0
            self.install_button.state(["!disabled"])
            self.finished = False
            self.worker = None
            self.cancel.clear()
            for widget in self.body.winfo_children():
                try:
                    widget.state(["!disabled"])
                except (AttributeError, self.tk.TclError):
                    pass
            return
        if not ok:
            self.show_error(value)
            self.install_button.state(["!disabled"])
            return

        self.result = value
        self.bar["value"] = 1000
        self.status.set(f"{APP_NAME} {value.version} is installed.")
        self.detail.set(
            f"{value.install_dir}  —  {human_size(value.total_bytes)} "
            f"in {value.seconds:.0f} seconds")
        self.cancel_button.destroy()
        self.install_button.destroy()
        self.ttk.Button(self.buttons, text="Close",
                        command=self.root.destroy).grid(row=0, column=0, padx=(0, 8))
        self.ttk.Button(self.buttons, text=f"Open {APP_NAME}",
                        command=self._launch).grid(row=0, column=1)

    def _launch(self) -> None:
        if self.result is not None:
            install_steps.launch(self.result.install_dir)
        self.root.destroy()


class UninstallWindow(Window):
    def __init__(self, plan: uninstall_steps.Plan) -> None:
        version = f" {plan.version}" if plan.version else ""
        super().__init__(
            f"Remove {APP_NAME}{version}",
            f"{plan.install_dir} — {human_size(plan.install_bytes)}")
        tk, ttk = self.tk, self.ttk
        self.plan = plan
        self.removed: uninstall_steps.Removed | None = None
        self.keep = tk.BooleanVar(value=True)

        if plan.data_dir is not None:
            ttk.Checkbutton(
                self.body,
                text=f"Keep my library and settings "
                     f"({human_size(plan.data_bytes)})",
                variable=self.keep).grid(row=0, column=0, sticky="w")
            ttk.Label(
                self.body, style="Muted.TLabel", wraplength=470, justify="left",
                text=(f"{plan.data_dir} holds what Mistery has scanned, what "
                      "you have watched and where you stopped, your artwork "
                      "cache, your friends and your TMDB key. Leaving it means a reinstall "
                      "picks up exactly where this one left off. Unticking "
                      "this deletes all of it, and it cannot be undone.")
            ).grid(row=1, column=0, sticky="w", pady=(2, 8))
        else:
            ttk.Label(self.body, style="Muted.TLabel",
                      text="There is no library folder to keep or remove."
                      ).grid(row=0, column=0, sticky="w", pady=(0, 8))

        what = []
        if plan.shortcuts:
            what.append(f"{len(plan.shortcuts)} shortcut"
                        f"{'s' if len(plan.shortcuts) > 1 else ''}")
        if plan.update_task:
            what.append("the hourly update check")
        if plan.sharing_entry:
            what.append("sharing with friends from sign-in")
        if plan.link_handler:
            what.append("opening Mistery from links")
        what.append("the Add/Remove Programs entry")
        ttk.Label(self.body, style="Muted.TLabel", wraplength=470, justify="left",
                  text="Also removed: " + ", ".join(what) + "."
                  ).grid(row=2, column=0, sticky="w")

        self.cancel_button = ttk.Button(self.buttons, text="Keep Mistery",
                                        command=self._on_close)
        self.cancel_button.grid(row=0, column=0, padx=(0, 8))
        self.go = ttk.Button(self.buttons, text="Remove", command=self._remove)
        self.go.grid(row=0, column=1)
        self.status.set("")

    def _remove(self) -> None:
        self.go.state(["disabled"])
        self.cancel_button.state(["disabled"])
        keep = self.keep.get()
        self.start(lambda: uninstall_steps.run(self.plan, keep, self.report))

    def on_done(self, ok, value) -> None:
        if ok is None:
            self.root.destroy()
            return
        if not ok:
            self.show_error(value)
            self.go.state(["!disabled"])
            self.cancel_button.state(["!disabled"])
            return
        self.removed = value
        self.bar["value"] = 1000
        self.status.set(f"{APP_NAME} has been removed.")
        if value.left_behind:
            self.detail.set("Left behind: " + ", ".join(value.left_behind[:2]))
        elif value.data_dir:
            self.detail.set("The library folder was deleted too.")
        else:
            self.detail.set("Your library and settings are still there.")
        self.cancel_button.destroy()
        self.go.destroy()
        self.ttk.Button(self.buttons, text="Close",
                        command=self.root.destroy).grid(row=0, column=0)


# --- no window at all -------------------------------------------------------


class _Nowhere:
    """A stdout for a program that has none. Writes vanish; nothing raises."""

    def write(self, text: str) -> int:
        return len(text)

    def flush(self) -> None:
        pass

    def isatty(self) -> bool:
        return False


def ensure_streams() -> None:
    """Make sure printing cannot crash the installer.

    MisterySetup.exe is built with --windowed, so double-clicking it shows a
    window instead of a console flashing behind one. The cost is that when
    nothing gave it a stdout, sys.stdout is None — and then --help, a progress
    line or a traceback dies with an AttributeError, which PyInstaller's
    windowed bootloader answers with a message box that nobody is standing
    there to click. That is a hung installer, and it is how this was found.

    So: borrow the console of whatever launched it if there is one, and
    otherwise let writes go nowhere.
    """
    if os.name == "nt" and is_frozen():
        import ctypes

        if ctypes.windll.kernel32.AttachConsole(-1):
            for name in ("stdout", "stderr"):
                if getattr(sys, name, None) is None:
                    try:
                        setattr(sys, name, open("CONOUT$", "w", buffering=1,
                                                encoding="utf-8",
                                                errors="replace"))
                    except OSError:
                        pass
    for name in ("stdout", "stderr"):
        if getattr(sys, name, None) is None:
            setattr(sys, name, _Nowhere())


class _Tee:
    """Write progress to the console and to --log at the same time."""

    def __init__(self, *streams) -> None:
        self._streams = [s for s in streams if s is not None]

    def write(self, text: str) -> int:
        for stream in self._streams:
            try:
                stream.write(text)
                stream.flush()
            except (OSError, ValueError):
                pass
        return len(text)

    def flush(self) -> None:
        for stream in self._streams:
            try:
                stream.flush()
            except (OSError, ValueError):
                pass


def _printing_report():
    """A report callback that prints, without one line per 8 MB chunk."""
    last = [0.0, ""]

    def report(what: str, fraction: float) -> None:
        now = time.monotonic()
        if what != last[1] or now - last[0] > 1.0:
            last[0], last[1] = now, what
            print(f"[{fraction * 100:5.1f}%] {what}", flush=True)

    return report


def _unattended_install(options: install_steps.Options) -> int:
    started = time.monotonic()
    try:
        result = install_steps.run(options, _printing_report())
    except Cancelled:
        print("cancelled")
        return 2
    except InstallError as error:
        print(f"\nfailed: {error}", file=sys.stderr)
        return 1
    print(f"\ninstalled {APP_NAME} {result.version} to {result.install_dir}")
    print(f"  app       {human_size(result.app_bytes)}")
    print(f"  runtime   {human_size(result.runtime_bytes)}")
    print(f"  total     {human_size(result.total_bytes)}")
    print(f"  shortcut  {result.start_menu or '(none)'}")
    print(f"  desktop   {result.desktop or '(none)'}")
    print(f"  task      {result.update_task or '(none)'}")
    print(f"  took      {time.monotonic() - started:.1f} s")
    for note in result.notes:
        print(f"  note      {note}")
    if options.launch_when_done:
        install_steps.launch(result.install_dir)
    return 0


def _unattended_uninstall(folder: Path | None, keep_data: bool) -> int:
    try:
        plan = uninstall_steps.read_plan(uninstall_steps.find_install_dir(folder))
        removed = uninstall_steps.run(plan, keep_data, _printing_report())
    except InstallError as error:
        print(f"failed: {error}", file=sys.stderr)
        return 1
    print(f"\nremoved {plan.install_dir}")
    print(f"  install folder  {'gone' if removed.install_dir else 'still there'}")
    print(f"  shortcuts       {len(removed.shortcuts)} removed")
    print(f"  update task     {'gone' if removed.update_task else 'not removed'}")
    print(f"  sharing         {'stopped' if removed.sharer_stopped else 'was not running'}, "
          f"sign-in entry {'gone' if removed.sharing_entry else 'none of this install'}")
    print(f"  mistery:// links {'gone' if removed.link_handler else 'none of this install'}")
    print(f"  ARP entry       {'gone' if removed.arp_entry else 'not removed'}")
    print(f"  library data    "
          f"{'deleted' if removed.data_dir else 'kept at ' + str(plan.data_dir)}")
    for item in removed.left_behind:
        print(f"  left behind     {item}")
    return 0


# --- the command line -------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="MisterySetup", add_help=True,
        description=f"Install or remove {APP_NAME}.")
    parser.add_argument("--dir", type=Path, default=None,
                        help="where to install (default "
                             "%%LOCALAPPDATA%%\\Programs\\Mistery)")
    parser.add_argument("--payload", type=Path, default=None,
                        help="install from this build folder (only when running "
                             "from source; MisterySetup.exe refuses it)")
    parser.add_argument("--unattended", action="store_true",
                        help="no window: install with the defaults and print "
                             "what happens")
    parser.add_argument("--uninstall", action="store_true",
                        help="remove an installed Mistery")
    parser.add_argument("--quiet", action="store_true",
                        help="with --uninstall: remove without asking, keeping "
                             "the library")
    parser.add_argument("--delete-data", action="store_true",
                        help="with --uninstall --quiet: delete "
                             "%%APPDATA%%\\Mistery as well")
    parser.add_argument("--desktop", action="store_true",
                        help="also put a shortcut on the Desktop")
    parser.add_argument("--no-start-menu", action="store_true")
    parser.add_argument("--no-task", action="store_true",
                        help="do not register the hourly update check")
    parser.add_argument("--launch", action="store_true",
                        help="start Mistery when the install finishes")
    parser.add_argument("--keep-archives", action="store_true",
                        help="keep the downloaded mpv/ffmpeg archives in "
                             "update\\ (for testing a second install offline)")
    parser.add_argument("--refetch-tools", action="store_true",
                        help="download mpv and ffmpeg again even if they are "
                             "already there")
    parser.add_argument("--log", type=Path, default=None,
                        help="also write the progress lines to this file")
    parser.add_argument("--from-temp", type=Path, default=None,
                        help=argparse.SUPPRESS)   # set when it relaunches itself
    return parser


def runs_as_uninstaller() -> bool:
    """True when this exe is the copy that was installed as Uninstall.exe.

    Uninstall.exe is MisterySetup.exe under another name, and the name is the
    only thing that tells them apart — because Windows runs the UninstallString
    from Add/Remove Programs with **no arguments at all**. Without this, clicking
    Uninstall in Settings > Apps opened the install window, which is what it did
    until an installer sat there for four minutes with a window nobody could see
    because the test harness had captured its output.

    Also matches MisteryUninstall-<pid>.exe, the copy the uninstaller makes of
    itself in %TEMP% so that it can delete the folder it was running from.
    """
    return is_frozen() and "uninstall" in Path(sys.executable).stem.lower()


def main(argv: list[str] | None = None) -> int:
    ensure_streams()
    args = build_parser().parse_args(argv)
    if runs_as_uninstaller():
        args.uninstall = True
    if args.log is not None:
        try:
            args.log.parent.mkdir(parents=True, exist_ok=True)
            handle = open(args.log, "a", encoding="utf-8", buffering=1)
            sys.stdout = _Tee(getattr(sys, "stdout", None), handle)
            sys.stderr = _Tee(getattr(sys, "stderr", None), handle)
        except OSError:
            pass
    if args.payload is not None:
        # MisterySetup.exe installs the copy of Mistery glued to the end of
        # itself, checked against the SHA-256 in its own trailer, and nothing
        # else. Said out loud rather than ignored quietly, because a test
        # harness that passes --payload to the frozen exe and gets a clean
        # install back would think it had tested something it had not.
        if is_frozen():
            print("--payload is for running the installer from source. "
                  "MisterySetup.exe installs the Mistery inside it.")
            return 2
        os.environ["MISTERY_SETUP_PAYLOAD"] = str(args.payload.resolve())

    if args.uninstall:
        return _uninstall(args)
    return _install(args)


def _install(args) -> int:
    options = install_steps.Options(
        install_dir=(args.dir.resolve() if args.dir else default_install_dir()),
        start_menu_shortcut=not args.no_start_menu,
        desktop_shortcut=args.desktop,
        register_update_task=not args.no_task,
        launch_when_done=args.launch,
        keep_archives=args.keep_archives,
        refetch_tools=args.refetch_tools,
    )
    if args.unattended:
        return _unattended_install(options)
    window = InstallWindow(options)
    window.run()
    return 0 if window.result is not None else 3


def _uninstall(args) -> int:
    target = args.dir
    # Uninstall.exe lives in the folder it is about to delete, and Windows will
    # not delete a file it is running. So it copies itself out first and the
    # copy does the work. --from-temp is that copy saying "I am the copy".
    if args.from_temp is None and is_frozen():
        here = Path(sys.executable).resolve().parent
        if target is None:
            target = here
        if Path(sys.executable).resolve().parent == target.resolve():
            forwarded = ["--uninstall", "--dir", str(target),
                         "--from-temp", str(target)]
            if args.quiet:
                forwarded.append("--quiet")
            if args.delete_data:
                forwarded.append("--delete-data")
            if uninstall_steps.relaunch_from_temp(forwarded):
                return 0

    try:
        folder = uninstall_steps.find_install_dir(target)
        plan = uninstall_steps.read_plan(folder)
    except InstallError as error:
        if args.quiet or args.unattended:
            print(f"failed: {error}", file=sys.stderr)
            return 1
        from tkinter import Tk, messagebox
        root = Tk()
        root.withdraw()
        messagebox.showerror(f"{APP_NAME} Setup", str(error))
        root.destroy()
        return 1

    if args.quiet or args.unattended:
        code = _unattended_uninstall(folder, keep_data=not args.delete_data)
        went_through = code == 0
    else:
        window = UninstallWindow(plan)
        window.run()
        # "Keep Mistery" closes the window without removing anything, and the
        # sweep below must not then delete the folder anyway.
        went_through = window.removed is not None
        code = 0

    if args.from_temp is not None and went_through:
        uninstall_steps.finish_after_exit(Path(args.from_temp))
    if args.from_temp is not None:
        uninstall_steps.cleanup_self(Path(sys.executable).resolve())
    return code


def guarded_main(argv: list[str] | None = None) -> int:
    """main(), with nothing allowed to escape as an unhandled exception.

    An unhandled exception in a --windowed PyInstaller build becomes a modal
    dialog: the process sits there until somebody clicks it, which on a
    scheduled or scripted run is forever. Anything that gets this far is
    written down where it can be read and then reported normally.
    """
    ensure_streams()
    try:
        return main(argv)
    except SystemExit as stop:                  # argparse --help, and exit codes
        return int(stop.code or 0)
    except KeyboardInterrupt:
        return 2
    except BaseException as error:              # noqa: BLE001 — written, not hidden
        import traceback

        report = traceback.format_exc()
        crash = Path(os.environ.get("TEMP", ".")) / "MisterySetup-crash.txt"
        try:
            crash.write_text(report, encoding="utf-8")
        except OSError:
            crash = Path("(nowhere writable)")
        print(report, file=sys.stderr)
        try:
            from tkinter import Tk, messagebox

            root = Tk()
            root.withdraw()
            messagebox.showerror(
                f"{APP_NAME} Setup",
                f"{APP_NAME} Setup stopped with an error it did not expect:\n\n"
                f"{type(error).__name__}: {error}\n\n"
                f"The details are in {crash}.")
            root.destroy()
        except Exception:                       # noqa: BLE001 — already failing
            pass
        return 1


if __name__ == "__main__":
    raise SystemExit(guarded_main())
