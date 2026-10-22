"""Read big CAD files in a separate process.

OCCT holds Python's GIL while it reads, so reading a large STEP on the GUI thread freezes the window. The
worker process reads the file, packs each distinct part's geometry once (repeated parts share it) and sends it
back with the instance list; the GUI stays live, shows progress and can cancel. A crash on a bad file only takes
the worker down."""
from __future__ import annotations

import multiprocessing as mp
import os
import queue as _queue
import time

BACKGROUND_MIN_BYTES = 3 * 1024 * 1024        # smaller files: reading in-process is faster than starting a worker
BACKGROUND_KINDS = {"step", "iges", "brep", "stl", "obj", "3mf"}


def _work(path, q):
    try:
        from daedalus.io import formats

        def prog(stage, i=0, n=0):
            q.put(("p", stage, i, n))
        res = formats.import_file(path, prog)
        prog("Packing geometry")
        blobs, parts = {}, []
        for k, p in enumerate(res.parts):
            key = p.proto or f"part{k}"
            if key not in blobs:
                blobs[key] = formats.shape_to_text(p.shape)
            parts.append((p.name, key, [float(v) for v in p.pose], p.color, bool(p.fixed)))
        q.put(("done", {"kind": res.kind, "parts": parts, "blobs": blobs, "sketch": res.sketch,
                        "notes": list(res.notes), "mates": res.mates}))
    except Exception as e:  # noqa: BLE001
        q.put(("error", f"{type(e).__name__}: {e}"))


def wants_background(path) -> bool:
    from .formats import IMPORT_EXT
    from pathlib import Path
    kind = IMPORT_EXT.get(Path(path).suffix.lower())
    try:
        return kind in BACKGROUND_KINDS and os.path.getsize(path) >= BACKGROUND_MIN_BYTES
    except OSError:
        return False


def to_result(payload):
    """Worker payload → ImportResult whose parts carry their serialized geometry (`blob`)."""
    from .formats import ImportedPart, ImportResult
    res = ImportResult(payload["kind"], sketch=payload.get("sketch"), notes=payload.get("notes") or [],
                       mates=payload.get("mates"))
    for name, key, pose, color, fixed in payload["parts"]:
        res.parts.append(ImportedPart(name, None, pose, color, fixed, proto=key, blob=payload["blobs"][key]))
    return res


class ImportJob:
    """Start with start(); call poll() regularly (e.g. from a QTimer): returns ("progress", stage, i, n),
    ("done", ImportResult), ("error", message) or None."""

    def __init__(self, path):
        self.path = str(path)
        self.proc = None
        self.q = None
        self.started = None
        self.stage = "Starting"

    def start(self):
        ctx = mp.get_context("spawn")
        self.q = ctx.Queue()
        self.proc = ctx.Process(target=_work, args=(self.path, self.q), daemon=True)
        self.proc.start()
        self.started = time.time()

    def poll(self):
        last = None
        while True:
            try:
                msg = self.q.get_nowait()
            except _queue.Empty:
                break
            if msg[0] == "p":
                self.stage = msg[1]
                last = ("progress", msg[1], msg[2], msg[3])
            elif msg[0] == "done":
                self._finish()
                return ("done", to_result(msg[1]))
            else:
                self._finish()
                return ("error", msg[1])
        if last is None and self.proc is not None and not self.proc.is_alive():
            try:                                              # it may have sent its last message just now
                msg = self.q.get(timeout=0.5)
                if msg[0] == "done":
                    self._finish()
                    return ("done", to_result(msg[1]))
                if msg[0] == "error":
                    self._finish()
                    return ("error", msg[1])
            except _queue.Empty:
                pass
            self._finish()
            return ("error", f"The import worker stopped (exit code {self.proc.exitcode}) — the file may be "
                             "damaged or too large for memory")
        return last

    def elapsed(self):
        return time.time() - self.started if self.started else 0.0

    def cancel(self):
        if self.proc is not None and self.proc.is_alive():
            self.proc.terminate()
        self._finish()

    def _finish(self):
        try:
            if self.proc is not None:
                self.proc.join(timeout=2)
        except Exception:  # noqa: BLE001
            pass


def read_blocking(path, on_progress=None, sleep=0.05):
    """Background read without Qt (tests / scripts)."""
    job = ImportJob(path)
    job.start()
    while True:
        r = job.poll()
        if r is None:
            time.sleep(sleep)
            continue
        if r[0] == "progress":
            if on_progress:
                on_progress(*r[1:])
            continue
        if r[0] == "error":
            raise RuntimeError(r[1])
        return r[1]
