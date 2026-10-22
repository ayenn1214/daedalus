"""Setup check: is this Python able to run Daedalus, and is the Icarus agent (Ollama + a model) ready?

Used three ways:
  python -m daedalus --check          prints the report (exit code 1 if something required is missing)
  install.bat / install.ps1           runs it after installing, to confirm
  Help ▸ Setup check…                 the same report in a dialog, with Install / Pull buttons

Qt-free; checks never install anything — the install_* / pull_* helpers do, when asked.
"""
from __future__ import annotations

import importlib
import importlib.util
import json
import os
import platform
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

PY_WANT = (3, 11)                    # the sketch solver (python-solvespace) ships Windows wheels for 3.10 / 3.11
PY_OK = ((3, 10), (3, 11), (3, 12), (3, 13))

# requirement (pip name) → import name; optional ones don't fail the check
REQUIRED = [("build123d", "build123d"), ("PySide6", "PySide6"), ("vtk", "vtkmodules"), ("anthropic", "anthropic"),
            ("python-solvespace", "python_solvespace"), ("ezdxf", "ezdxf"), ("mujoco", "mujoco"), ("scipy", "scipy"),
            ("gmsh", "gmsh"), ("scikit-fem", "skfem"), ("numba", "numba"), ("openpyxl", "openpyxl"),
            ("shapely", "shapely"), ("svgelements", "svgelements"), ("qtawesome", "qtawesome")]
OPTIONAL = [("coacd", "coacd", "exact collision shapes in physics"),
            ("pywin32", "win32com", "CAD Link to SolidWorks / Inventor / Solid Edge")]

OLLAMA_WIN_EXE = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))) / "Programs" / "Ollama" / \
    "ollama.exe"
OLLAMA_DOWNLOAD = "https://ollama.com/download"
# (minimum RAM GB, model) — picked from the PC's memory; 14b needs ~10 GB free for the model alone
MODEL_BY_RAM = [(24, "qwen2.5:14b"), (12, "qwen2.5:7b"), (0, "qwen2.5:3b")]
DEFAULT_HOST = "http://127.0.0.1:11434"    # same as ai.ollama (that module loads the CAD kernel: too slow here)
DEFAULT_MODEL = "qwen2.5:14b"


# ---------------------------------------------------------------- tiny Ollama client (stdlib only)
def normalize_host(h):
    h = (h or "").strip().rstrip("/") or DEFAULT_HOST
    if "://" not in h:
        h = "http://" + h
    scheme, rest = h.split("://", 1)
    hostport = rest.split("/", 1)[0]
    host, _, port = hostport.rpartition(":") if hostport.count(":") == 1 else (hostport, "", "")
    if not host:
        host, port = hostport, ""
    if host in ("0.0.0.0", "*", "[::]", "::"):
        host = "127.0.0.1"
    return f"{scheme}://{host}:{port or '11434'}"


def _api(host, path, payload=None, timeout=3.0):
    import urllib.request
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(host.rstrip("/") + path, data=data, method="POST" if data else "GET",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode() or "{}")


def list_models(host, timeout=3.0):
    try:
        got = _api(host, "/api/tags", timeout=timeout)
    except Exception:  # noqa: BLE001
        return []
    return sorted(m.get("name", "") for m in got.get("models", []) if m.get("name"))


def capabilities(host, model, timeout=6.0):
    try:
        return set(_api(host, "/api/show", {"model": model}, timeout=timeout).get("capabilities") or [])
    except Exception:  # noqa: BLE001
        return set()


@dataclass
class Row:
    group: str                 # "Python" | "Icarus agent"
    name: str
    ok: bool | None            # True ok · False needs fixing · None information
    detail: str
    fix: str = ""              # what to do (human words)
    action: str = ""           # "packages" | "ollama" | "start" | "pull" — what the dialog's button runs
    required: bool = True

    def to_dict(self):
        return asdict(self)


@dataclass
class Report:
    rows: list = field(default_factory=list)

    def add(self, *a, **k):
        self.rows.append(Row(*a, **k))
        return self.rows[-1]

    @property
    def ok(self) -> bool:
        return not any(r.ok is False and r.required for r in self.rows)

    def group(self, g):
        return [r for r in self.rows if r.group == g]

    def text(self) -> str:
        out, last = [], None
        for r in self.rows:
            if r.group != last:
                out.append(f"\n{r.group}")
                last = r.group
            mark = {True: "ok ", False: "!! " if r.required else "-- ", None: "   "}[r.ok]
            out.append(f"  [{mark}] {r.name}: {r.detail}" + (f"\n         fix: {r.fix}" if r.fix and r.ok is False else ""))
        out.append("\nAll set." if self.ok else "\nSomething required is missing — see the fixes above.")
        return "\n".join(out).lstrip("\n")

    def to_json(self) -> str:
        return json.dumps({"ok": self.ok, "rows": [r.to_dict() for r in self.rows]}, indent=1)


# ---------------------------------------------------------------- system facts
def ram_gb() -> float | None:
    try:
        if sys.platform == "win32":
            import ctypes

            class MS(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
            m = MS()
            m.dwLength = ctypes.sizeof(MS)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
            return m.ullTotalPhys / 1024 ** 3
        if Path("/proc/meminfo").exists():
            for line in Path("/proc/meminfo").read_text().splitlines():
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) / 1024 ** 2
        if sys.platform == "darwin":
            out = subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True, timeout=5).stdout
            return int(out.strip()) / 1024 ** 3
    except Exception:  # noqa: BLE001
        pass
    return None


def recommended_model(ram: float | None = None) -> str:
    ram = ram_gb() if ram is None else ram
    if ram is None:
        return MODEL_BY_RAM[0][1]
    return next(m for need, m in MODEL_BY_RAM if ram >= need)


def ollama_exe() -> str | None:
    found = shutil.which("ollama")
    if found:
        return found
    if sys.platform == "win32" and OLLAMA_WIN_EXE.exists():
        return str(OLLAMA_WIN_EXE)
    for p in ("/usr/local/bin/ollama", "/usr/bin/ollama", "/Applications/Ollama.app/Contents/Resources/ollama"):
        if Path(p).exists():
            return p
    return None


def _settings():
    try:
        from PySide6.QtCore import QSettings
        return QSettings("Daedalus", "Daedalus")
    except Exception:  # noqa: BLE001
        return None


def agent_config() -> tuple[str, str]:
    """(host, model) Icarus will use: the saved settings, else the defaults."""
    s = _settings()
    host = (s.value("ollama/host") if s is not None else None) or os.environ.get("OLLAMA_HOST")
    model = (s.value("ollama/model") if s is not None else None) or DEFAULT_MODEL
    return normalize_host(host), str(model)


# ---------------------------------------------------------------- checks
def check_python(rep: Report | None = None) -> Report:
    rep = rep or Report()
    g = "Python"
    v = sys.version_info[:2]
    bits = platform.architecture()[0]
    ok = v in PY_OK and bits == "64bit"
    rep.add(g, "Version", ok, f"{platform.python_version()} {bits} — {sys.executable}",
            fix="Install Python 3.11 64-bit (install.bat does it) — 3.11 has prebuilt wheels for every package")
    if sys.platform == "win32" and v != PY_WANT:
        rep.add(g, "Recommended version", None, "3.11 is the tested version on Windows (sketch solver wheels)",
                required=False)
    if "WindowsApps" in sys.executable:
        rep.add(g, "Install type", False, "Microsoft Store Python — sandboxed, the CAD Link can't reach other apps",
                fix="Install Python from python.org (install.bat does it)")
    venv = sys.prefix != getattr(sys, "base_prefix", sys.prefix)
    rep.add(g, "Environment", None, f"virtual environment {sys.prefix}" if venv else "system Python (no venv)",
            required=False)
    missing = []
    for pip_name, mod in REQUIRED:
        if importlib.util.find_spec(mod) is None:
            missing.append(pip_name)
    if missing:
        rep.add(g, "Packages", False, f"missing: {', '.join(missing)}",
                fix=f'"{sys.executable}" -m pip install -r requirements.txt', action="packages")
    else:
        rep.add(g, "Packages", True, f"all {len(REQUIRED)} required packages installed")
    for pip_name, mod, why in OPTIONAL:
        if pip_name == "pywin32" and sys.platform != "win32":
            continue
        have = importlib.util.find_spec(mod) is not None
        rep.add(g, pip_name, True if have else False, "installed" if have else f"not installed — {why}",
                fix=f'"{sys.executable}" -m pip install {pip_name}', action="packages", required=False)
    return rep


def check_agent(rep: Report | None = None, host: str | None = None, model: str | None = None,
                timeout: float = 3.0) -> Report:
    rep = rep or Report()
    g = "Icarus agent"
    h0, m0 = agent_config()
    host, model = host or h0, model or m0
    ram = ram_gb()
    rec = recommended_model(ram)
    rep.add(g, "Memory", None, (f"{ram:.0f} GB RAM — recommended model {rec}" if ram else
                                f"unknown — recommended model {rec}"), required=False)
    exe = ollama_exe()
    local = host.startswith(("http://127.0.0.1", "http://localhost"))
    if local:
        rep.add(g, "Ollama installed", bool(exe), exe or "not found",
                fix="Install Ollama (install.bat -AgentOnly, or the button here)", action="ollama")
    models = list_models(host, timeout=timeout)
    up = bool(models) or _reachable(host, timeout)
    rep.add(g, "Ollama running", up, f"answering at {host}" if up else f"no answer at {host}",
            fix=("Start Ollama (Start menu ▸ Ollama, or the button here)" if local else
                 f"Start Ollama on the server at {host} (set OLLAMA_HOST=0.0.0.0 there)"),
            action="start" if (local and exe) else "")
    if up:
        have = model in models or f"{model}:latest" in models
        rep.add(g, "Model", have, f"{model} " + ("installed" if have else "not downloaded") +
                (f" ({len(models)} model(s) on the server)" if models else ""),
                fix=f"ollama pull {model}", action="pull")
        if have:
            caps = capabilities(host, model, timeout=timeout + 3)
            if caps:
                rep.add(g, "Tool calling", "tools" in caps,
                        "supported" if "tools" in caps else f"{model} can't call tools — Icarus needs a tool model",
                        fix="Use qwen2.5, llama3.1 or mistral-nemo", required=False)
                rep.add(g, "Images", None, "vision supported" if "vision" in caps else
                        "no vision (image-to-model needs a vision model, e.g. qwen2.5vl)", required=False)
    return rep


def _reachable(host, timeout):
    import urllib.request
    try:
        with urllib.request.urlopen(host.rstrip("/") + "/api/version", timeout=timeout):
            return True
    except Exception:  # noqa: BLE001
        return False


def full_report(agent=True) -> Report:
    rep = check_python()
    if agent:
        check_agent(rep)
    return rep


# ---------------------------------------------------------------- fixes (run only when asked)
def install_packages(log=print) -> bool:
    """pip install the requirements into *this* Python."""
    root = Path(__file__).resolve().parents[1]
    reqs = [root / "requirements.txt", root / "requirements-optional.txt"]
    ok = True
    for i, r in enumerate(reqs):
        if not r.exists():
            continue
        cmd = [sys.executable, "-m", "pip", "install", "--only-binary=:all:", "-r", str(r)]
        log(" ".join(cmd))
        p = subprocess.run(cmd, capture_output=True, text=True, **_no_window())
        log((p.stdout or "")[-1500:] + (p.stderr or "")[-1500:])
        if p.returncode != 0 and i == 0:
            ok = False
    return ok


def install_ollama(log=print) -> bool:
    """Windows: winget, else the official installer. Elsewhere: say how."""
    if sys.platform != "win32":
        log("Install Ollama: curl -fsSL https://ollama.com/install.sh | sh   (Linux) · or " + OLLAMA_DOWNLOAD)
        return False
    if shutil.which("winget"):
        cmd = ["winget", "install", "-e", "--id", "Ollama.Ollama", "--silent", "--accept-package-agreements",
               "--accept-source-agreements"]
        log(" ".join(cmd))
        p = subprocess.run(cmd, capture_output=True, text=True, **_no_window())
        log((p.stdout or "")[-1500:])
        if ollama_exe():
            return True
    import tempfile
    import urllib.request
    dest = Path(tempfile.gettempdir()) / "OllamaSetup.exe"
    url = "https://ollama.com/download/OllamaSetup.exe"
    log(f"downloading {url}")
    try:
        urllib.request.urlretrieve(url, dest)
    except Exception as e:  # noqa: BLE001
        log(f"download failed: {e} — get it from {OLLAMA_DOWNLOAD}")
        return False
    p = subprocess.run([str(dest), "/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART"], **_no_window())
    return bool(ollama_exe())


def start_ollama(host: str | None = None, wait: float = 20.0, log=print) -> bool:
    import time
    host = host or agent_config()[0]
    if _reachable(host, 2):
        return True
    exe = ollama_exe()
    if not exe:
        log("Ollama isn't installed")
        return False
    kw = _no_window()
    if sys.platform == "win32":
        kw["creationflags"] = kw.get("creationflags", 0) | 0x00000008          # DETACHED_PROCESS
    subprocess.Popen([exe, "serve"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kw)
    t0 = time.time()
    while time.time() - t0 < wait:
        if _reachable(host, 2):
            return True
        time.sleep(0.5)
    return False


def pull_model(model: str | None = None, host: str | None = None, progress=None, stop=None) -> str:
    """Download a model through the Ollama server (streamed). progress(status, done, total)."""
    import urllib.request
    h, m = agent_config()
    host, model = host or h, model or m
    req = urllib.request.Request(host.rstrip("/") + "/api/pull", method="POST",
                                 data=json.dumps({"model": model, "stream": True}).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        for line in r:
            if stop and stop():
                raise RuntimeError("Download cancelled")
            try:
                msg = json.loads(line.strip() or b"{}")
            except ValueError:
                continue
            if msg.get("error"):
                raise RuntimeError(f"Ollama: {msg['error']}")
            if progress and msg:
                progress(str(msg.get("status", "")), int(msg.get("completed") or 0), int(msg.get("total") or 0))
            if msg.get("status") == "success":
                break
    return model


def set_model(model: str):
    s = _settings()
    if s is not None:
        s.setValue("ollama/model", model)
        s.setValue("ai/provider", "ollama")
        s.sync()


def _no_window():
    return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)} if sys.platform == "win32" else {}


# ---------------------------------------------------------------- CLI
def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--set-model" in argv:
        i = argv.index("--set-model")
        name = argv[i + 1] if i + 1 < len(argv) else recommended_model()
        set_model(name)
        print(f"Icarus model set to {name}")
        return 0
    if "--recommended-model" in argv:
        print(recommended_model())
        return 0
    rep = check_python()
    if "--python-only" not in argv:
        check_agent(rep)
    print(rep.to_json() if "--json" in argv else rep.text())
    return 0 if rep.ok else 1
