"""Auto-update from the GitHub repo.

Every Daedalus (full app, the light Drawings / DIMS windows, CAD Link) checks the repo in the background at start
(at most once a day) and offers the update. Channels:
  releases  the newest GitHub Release (or tag) — stable, the default
  main      every commit pushed to main — for testing
Installs that are a git checkout update with `git pull`; zip installs download the new version, and a small helper
swaps the files in after Daedalus closes (the .venv, your settings and data are untouched; replaced files are kept
in .update/backup-<version> so you can roll back), installs any new requirements, and restarts Daedalus.
"""
from __future__ import annotations

import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

from . import __version__

DEFAULT_REPO = "ayenn1214/daedalus"
API = "https://api.github.com"


class UpdateError(RuntimeError):
    pass


def root() -> Path:
    return Path(__file__).resolve().parents[1]


def vtuple(v: str) -> tuple:
    nums = re.findall(r"\d+", v or "")
    return tuple(int(n) for n in nums[:4]) or (0,)


def build_info() -> dict:
    try:
        return json.loads((root() / ".update" / "build.json").read_text())
    except (OSError, ValueError):
        return {}


def current() -> dict:
    r = root()
    commit = ""
    is_git = (r / ".git").exists() and shutil.which("git") is not None
    if is_git:
        try:
            commit = subprocess.run(["git", "-C", str(r), "rev-parse", "HEAD"], capture_output=True, text=True,
                                    timeout=10).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            commit = ""
    return {"version": __version__, "commit": commit or build_info().get("commit", ""), "git": is_git,
            "git_dir": (r / ".git").exists(), "root": str(r)}


def _get(url, token=None, raw=False, timeout=20):
    h = {"User-Agent": f"Daedalus/{__version__}", "Accept": "application/vnd.github+json"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=timeout) as r:
            data = r.read()
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise UpdateError("Repository or release not found (private repo? add a GitHub token)") from e
        if e.code == 403:
            raise UpdateError("GitHub rate limit reached — try again in an hour") from e
        raise UpdateError(f"GitHub answered {e.code}") from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise UpdateError(f"Can't reach GitHub ({getattr(e, 'reason', e)})") from e
    return data if raw else json.loads(data)


def latest(repo: str = DEFAULT_REPO, channel: str = "releases", token: str | None = None) -> dict:
    """What the repo offers: {version, ref, commit, notes, url, zip}."""
    if channel == "main":
        c = _get(f"{API}/repos/{repo}/commits/main", token)
        sha = c["sha"]
        init = _get(f"https://raw.githubusercontent.com/{repo}/{sha}/daedalus/__init__.py", token, raw=True)
        m = re.search(rb'__version__\s*=\s*"([^"]+)"', init)
        return {"version": m.group(1).decode() if m else "0", "ref": sha, "commit": sha, "channel": "main",
                "notes": c["commit"]["message"], "url": c.get("html_url", ""),
                "zip": f"https://codeload.github.com/{repo}/zip/{sha}"}
    try:
        r = _get(f"{API}/repos/{repo}/releases/latest", token)
        tag = r["tag_name"]
        notes, url = r.get("body") or "", r.get("html_url", "")
    except UpdateError:
        tags = _get(f"{API}/repos/{repo}/tags", token)
        if not tags:
            raise UpdateError("The repo has no releases or tags yet")
        tags.sort(key=lambda t: vtuple(t["name"]), reverse=True)
        tag, notes, url = tags[0]["name"], "", f"https://github.com/{repo}/releases/tag/{tags[0]['name']}"
    return {"version": tag.lstrip("vV"), "ref": tag, "commit": "", "channel": "releases", "notes": notes,
            "url": url, "zip": f"https://codeload.github.com/{repo}/zip/refs/tags/{tag}"}


def is_newer(info: dict, cur: dict | None = None) -> bool:
    cur = cur or current()
    if vtuple(info["version"]) > vtuple(cur["version"]):
        return True
    if info.get("channel") == "main" and info.get("commit") and cur.get("commit"):
        return info["commit"] != cur["commit"] and vtuple(info["version"]) >= vtuple(cur["version"])
    return False


def check(settings=None, force=False) -> dict | None:
    """Background-safe. Returns the update info when one is available (and not skipped), else None. Without
    force, checks at most once every 20 h (remembered in settings)."""
    s = settings
    repo = (s.value("update/repo") if s else None) or DEFAULT_REPO
    channel = (s.value("update/channel") if s else None) or "releases"
    token = (s.value("update/token") if s else None) or os.environ.get("DAEDALUS_GITHUB_TOKEN")
    if s is not None and not force:
        last = float(s.value("update/last_check") or 0)
        if time.time() - last < 20 * 3600:
            cached = s.value("update/available") or ""
            try:
                info = json.loads(cached) if cached else None
            except ValueError:
                info = None
            return info if info and is_newer(info) and info["version"] != s.value("update/skip") else None
    info = latest(repo, channel, token)
    if s is not None:
        s.setValue("update/last_check", time.time())
        s.setValue("update/available", json.dumps(info) if is_newer(info) else "")
    if not is_newer(info):
        return None
    if s is not None and not force and info["version"] == s.value("update/skip") and info.get("channel") != "main":
        return None
    return info


# ------------------------------------------------------------------ installing
def git_update(info: dict) -> str:
    """git checkout: fast-forward to the release tag / main. Refuses when there are local changes."""
    r = str(root())

    def git(*a):
        p = subprocess.run(["git", "-C", r, *a], capture_output=True, text=True, timeout=300)
        if p.returncode:
            raise UpdateError((p.stderr or p.stdout).strip().splitlines()[-1] if (p.stderr or p.stdout) else
                              f"git {a[0]} failed")
        return p.stdout
    if git("status", "--porcelain", "--untracked-files=no").strip():
        raise UpdateError("This copy has local changes — commit or discard them first (GitHub Desktop)")
    git("fetch", "--tags", "origin")
    if info.get("channel") == "main":
        git("checkout", "main")
        git("merge", "--ff-only", "origin/main")
    else:
        git("checkout", "--detach", info["ref"])
    return git("rev-parse", "HEAD").strip()


def download(info: dict, token: str | None = None, progress=None) -> Path:
    """Download + unpack the new version into .update/stage-<version>. Returns the staged folder."""
    data = _get(info["zip"], token, raw=True, timeout=300)
    if progress:
        progress(len(data))
    stage = root() / ".update" / f"stage-{info['version']}"
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(parents=True)
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as e:
        raise UpdateError("The download isn't a valid zip") from e
    names = zf.namelist()
    top = names[0].split("/")[0] + "/" if names else ""
    for n in names:
        rel = n[len(top):] if n.startswith(top) else n
        if not rel or rel.endswith("/"):
            continue
        dest = (stage / rel).resolve()
        if not str(dest).startswith(str(stage.resolve()) + os.sep):        # no paths outside the stage
            raise UpdateError(f"Unsafe path in the download: {n}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(zf.read(n))
    if not (stage / "daedalus" / "__init__.py").exists():
        raise UpdateError("The download doesn't look like Daedalus")
    (stage / ".daedalus-update.json").write_text(json.dumps(info))
    return stage


def apply_now(stage: Path, target: Path | None = None, partial: bool = False) -> dict:
    """Swap the staged files into the install (used by the helper after Daedalus has closed; also by tests).
    Replaced / removed files go to .update/backup-<old version>. Returns {copied, removed, requirements_changed}."""
    target = Path(target or root())
    return _apply(str(stage), str(target), installed_version(target), partial)


def installed_version(target: Path) -> str:
    """The version of the files in an install folder (its daedalus/__init__.py), else the running one."""
    import re as _re
    try:
        m = _re.search(r'__version__\s*=\s*"([^"]+)"', (Path(target) / "daedalus" / "__init__.py").read_text())
        return m.group(1) if m else __version__
    except OSError:
        return __version__


def _apply(stage, target, old_version, partial=False):
    """Self-contained (the helper runs a copy of this module's text, outside the package it replaces)."""
    import filecmp
    import shutil as sh
    from pathlib import Path as P
    stage, target = P(stage), P(target)
    keep = {".venv", ".git", ".update", "daedalus.ico", "__pycache__", ".pytest_cache"}
    backup = target / ".update" / f"backup-{old_version}"
    new_files = {p.relative_to(stage) for p in stage.rglob("*") if p.is_file()
                 and not any(part in keep for part in p.relative_to(stage).parts)
                 and p.name != ".daedalus-update.json"}
    old_files = {p.relative_to(target) for p in (target / "daedalus").rglob("*") if p.is_file()
                 and not any(part in keep for part in p.relative_to(target).parts)}
    req_changed = not (target / "requirements.txt").exists() or not filecmp.cmp(
        stage / "requirements.txt", target / "requirements.txt", shallow=False) \
        if (stage / "requirements.txt").exists() else False
    copied = removed = 0
    for rel in sorted(new_files):
        src, dst = stage / rel, target / rel
        if dst.exists():
            if filecmp.cmp(src, dst, shallow=False):
                continue
            (backup / rel).parent.mkdir(parents=True, exist_ok=True)
            sh.copy2(dst, backup / rel)
        dst.parent.mkdir(parents=True, exist_ok=True)
        sh.copy2(src, dst)
        copied += 1
    for rel in sorted(set() if partial else old_files - new_files):   # modules deleted upstream (package only)
        if rel.suffix in (".pyc",):
            continue
        (backup / rel).parent.mkdir(parents=True, exist_ok=True)
        sh.move(str(target / rel), str(backup / rel))
        removed += 1
    return {"copied": copied, "removed": removed, "requirements_changed": req_changed}


HELPER = r'''
import json, os, subprocess, sys, time
args = json.loads(sys.argv[1])
pid = args["pid"]
alive = True
for _ in range(3600):                                    # wait for Daedalus to close (≤ 30 min)
    try:
        if os.name == "nt":
            import ctypes
            h = ctypes.windll.kernel32.OpenProcess(0x100000, False, pid)
            if not h:
                alive = False
                break
            r = ctypes.windll.kernel32.WaitForSingleObject(h, 500)
            ctypes.windll.kernel32.CloseHandle(h)
            if r == 0:
                alive = False
                break
        else:
            os.kill(pid, 0)
            time.sleep(0.5)
    except OSError:
        alive = False
        break
os.makedirs(os.path.join(args["target"], ".update"), exist_ok=True)
log = open(os.path.join(args["target"], ".update", "last-update.log"), "w", encoding="utf-8")
if alive:                                                # never swap files under a running Daedalus
    log.write("Daedalus didn't close within 30 min — update not applied\n")
    log.close()
    sys.exit(0)
time.sleep(1.0)                                          # let Windows release file handles
try:
    ns = {}
    exec(args["apply_src"], ns)
    res = ns["_apply"](args["stage"], args["target"], args["old"], args.get("partial", False))
    log.write(json.dumps(res) + "\n")
    if res["requirements_changed"]:
        subprocess.run([args["python"], "-m", "pip", "install", "-q", "-r",
                        os.path.join(args["target"], "requirements.txt")], cwd=args["target"], stdout=log, stderr=log)
    with open(os.path.join(args["target"], ".update", "build.json"), "w") as f:
        json.dump({"version": args["new"], "commit": args.get("commit", ""), "at": time.time()}, f)
    log.write("ok\n")
except Exception as e:
    log.write("FAILED: %r\n" % (e,))
if args.get("relaunch"):
    try:
        flags = 0x00000008 | 0x00000200 if os.name == "nt" else 0     # DETACHED_PROCESS | NEW_PROCESS_GROUP
        subprocess.Popen([args["python_gui"], "-m", "daedalus", *args["relaunch_args"]], cwd=args["target"],
                         creationflags=flags, start_new_session=os.name != "nt", close_fds=True)
        log.write("restarted\n")
    except Exception as e:
        log.write("RESTART FAILED: %r\n" % (e,))
log.close()
'''


def launch_helper(stage: Path, info: dict, relaunch=True, relaunch_args=None, partial=False) -> None:
    """Start the helper that applies the staged update once this process exits (then restarts Daedalus)."""
    import inspect
    apply_src = inspect.getsource(_apply)
    tmp = Path(tempfile.mkdtemp(prefix="daedalus-update-")) / "apply_update.py"
    tmp.write_text(HELPER, encoding="utf-8")
    py = sys.executable
    if os.name == "nt" and py.lower().endswith("python.exe") and Path(py[:-10] + "pythonw.exe").exists():
        py_gui = py[:-10] + "pythonw.exe"
    else:
        py_gui = py
    args = {"pid": os.getpid(), "stage": str(stage), "target": str(root()), "old": __version__,
            "new": info["version"], "commit": info.get("commit", ""), "apply_src": apply_src, "python": py,
            "python_gui": py_gui, "relaunch": relaunch, "relaunch_args": list(relaunch_args or []),
            "partial": partial}
    flags = 0x00000008 | 0x00000200 if os.name == "nt" else 0     # DETACHED_PROCESS | NEW_PROCESS_GROUP
    subprocess.Popen([py_gui, "-I", str(tmp), json.dumps(args)], cwd=tempfile.gettempdir(),
                     creationflags=flags, start_new_session=os.name != "nt", close_fds=True)


def rollback_folders() -> list[Path]:
    return sorted((root() / ".update").glob("backup-*"), reverse=True)


def rollback(folder: Path, relaunch=True, relaunch_args=None):
    """Put back the files an update replaced (applied after Daedalus closes, like an update)."""
    v = folder.name.removeprefix("backup-")
    launch_helper(folder, {"version": v, "commit": ""}, relaunch, relaunch_args, partial=True)
