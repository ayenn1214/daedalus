import sys


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    flags = {a for a in sys.argv[1:] if a.startswith("--")}
    if flags & {"--check", "--set-model", "--recommended-model"}:     # setup check (no window, no CAD kernel)
        from .setup_check import main as check
        sys.exit(check(sys.argv[1:]))
    if "--make-icon" in flags:                       # installer: write the logo as an .ico for shortcuts
        from PySide6.QtGui import QGuiApplication
        _app = QGuiApplication(sys.argv[:1])
        from .ui.splash import logo_pixmap
        out = sys.argv[sys.argv.index("--make-icon") + 1]
        ok = logo_pixmap(256).toImage().save(out)
        print(out if ok else "could not write the icon")
        sys.exit(0 if ok else 1)
    cad = next((a for a in ("fusion", "rhino", "inventor", "solidedge") if f"--{a}" in flags),
               "solidworks" if "--sw" in flags else None)
    if cad:                                          # CAD Link (add --demo for a simulated session)
        from .sw.ui import run
        sys.exit(run(demo="--demo" in flags, app=cad))
    if "--install-fusion-addin" in flags:
        from .fusion import install
        print(f"Installed: {install()}")
        return

    from PySide6.QtWidgets import QApplication

    from . import APP_NAME, __version__

    app = QApplication(sys.argv[:1])
    app.setApplicationName(APP_NAME)
    try:
        from .ui.splash import logo_icon
        app.setWindowIcon(logo_icon())
    except Exception:  # noqa: BLE001
        pass
    if sys.platform == "win32":                       # own taskbar group + icon instead of python.exe's
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Daedalus.CAD")
        except Exception:  # noqa: BLE001
            pass
    splash = _splash(__version__)

    def step(msg):
        if splash is not None:
            splash.showMessage(msg)
            app.processEvents()

    step("Loading geometry kernel…")
    from .ui import theme
    from .migrate import migrate_settings
    migrate_settings()
    theme.apply(app)
    from .ui.main_window import MainWindow
    step("Building workspace…")
    w = MainWindow()
    w.show()
    w.view.start()
    if splash is not None:
        splash.finish(w)
    _startup(w, args)
    sys.exit(app.exec())


def _startup(w, args):
    """File on the command line → open it; else reopen the last file if asked; else the Home screen."""
    from pathlib import Path
    s = w.settings
    if args:
        w.open_path(args[0], confirmed=True)
        return
    try:
        orphans = w.autosave.orphans()
    except Exception:  # noqa: BLE001
        orphans = []
    last = s.value("home/last")
    if not orphans and s.value("home/reopen_last", False, type=bool) and last and Path(last).exists():
        w.open_path(last, confirmed=True)
        return
    if orphans or s.value("home/show", True, type=bool):
        w.show_home()


def _splash(version):
    try:
        from .ui.splash import Splash
        return Splash(version)
    except Exception:  # noqa: BLE001
        return None

if __name__ == "__main__":
    main()
