"""Team inventory on Supabase — the same project and `app_storage` table the original web app used, so the
desktop app and the old web page can share data while you switch over. No Netlify: Daedalus talks to Supabase
directly over HTTPS.

* Sign-in: Supabase Authentication (email + password). Users are created in the Supabase dashboard.
* Saving: only the collections that changed are written, each with a version check (its `updated_at`). If someone
  else saved first, their data is fetched and merged record by record (see merge.py), then written.
* Other people's changes: a light check every few seconds (keys + timestamps only), then a fetch of what changed.
* Offline: the last data and any unsaved edits are kept in a local cache file and sent when the server is back.

Only the public "anon" key and the user's refresh token are stored on this PC — never the password.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .merge import merge_value
from .model import Inventory
from .store import default_path

TABLE = "app_storage"
SHARED_KEYS = ("parts", "transactions", "projects", "locations", "vendors", "production-orders", "clients",
               "client-orders", "part-orders", "exchange-rate", "sync-meta")


class SyncError(OSError):
    """Network / server problem — edits are kept and retried."""


class AuthError(SyncError):
    """Not signed in, or the session expired — sign in again."""


class SupabaseStore:
    """Same interface as InventoryStore (inv, load, save, check_external, listeners) plus sign-in."""

    kind = "supabase"
    layout = "blobs"

    def __init__(self, url: str, anon_key: str, session: dict | None = None, cache: str | Path | None = None,
                 on_session=None, timeout: float = 15.0):
        if not url or not anon_key:
            raise ValueError("Supabase URL and anon key are required")
        self.url = url.rstrip("/")
        self.key = anon_key
        self.session = dict(session or {})
        self.on_session = on_session              # called with the session dict when it changes (to persist it)
        self.timeout = timeout
        self.cache = Path(cache) if cache else default_path().with_name("dims-team-cache.json")
        if not cache:
            old = self.cache.with_name("holocron-team-cache.json")      # cache from before the rename
            if old.exists() and not self.cache.exists():
                try:
                    old.replace(self.cache)
                except OSError:
                    pass
        self.inv = Inventory()
        self.listeners = []
        self.base: dict = {}                       # values as last synced
        self.versions: dict = {}                   # key → updated_at on the server
        self.online = False
        self.last_sync: float | None = None
        self.last_error = ""
        self.conflicts: list = []
        self._failed_at = 0.0
        self._load_cache()

    # ------------------------------------------------------------ display
    def label(self) -> str:
        host = urllib.parse.urlparse(self.url).hostname or self.url
        return f"Team server ({host.split('.')[0]})"

    def location(self) -> str:
        return self.url

    def saved_at(self) -> float | None:
        return self.last_sync

    def pending(self) -> list[str]:
        """Keys with edits not yet on the server."""
        mine = self.inv.dump_values()
        return [k for k in SHARED_KEYS if k in mine and mine[k] != self.base.get(k)]

    @property
    def email(self) -> str:
        return (self.session.get("user") or {}).get("email", "")

    @property
    def signed_in(self) -> bool:
        return bool(self.session.get("refresh_token"))

    # ------------------------------------------------------------- HTTP
    def _req(self, method, path, body=None, headers=None, auth=True):
        h = {"apikey": self.key, "Content-Type": "application/json", "Accept": "application/json"}
        if auth:
            h["Authorization"] = f"Bearer {self._token()}"
        h.update(headers or {})
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.url + path, data=data, method=method, headers=h)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                raw = r.read()
                return r.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                msg = json.loads(raw)
            except ValueError:
                msg = raw.decode(errors="replace")
            if e.code == 401 or (isinstance(msg, dict) and msg.get("code") == "PGRST301"):
                raise AuthError("Session expired — sign in again") from e
            return e.code, msg
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            self.online = False
            raise SyncError(f"Can't reach the team server ({getattr(e, 'reason', e)})") from e

    def _rest(self, method, path, body=None, headers=None):
        try:
            return self._req(method, path, body, headers)
        except AuthError:
            if not self._refresh():
                raise
            return self._req(method, path, body, headers)

    # ------------------------------------------------------------- auth
    def sign_in(self, email: str, password: str) -> dict:
        code, res = self._req("POST", "/auth/v1/token?grant_type=password", {"email": email, "password": password},
                              auth=False)
        if code != 200 or not isinstance(res, dict) or "access_token" not in res:
            msg = (res or {}).get("error_description") or (res or {}).get("msg") or (res or {}).get("message") \
                if isinstance(res, dict) else str(res)
            raise AuthError(msg or "Sign-in failed")
        self._set_session(res)
        name = ((res.get("user") or {}).get("user_metadata") or {}).get("full_name") or email.split("@")[0]
        if not self.inv.userName:
            self.inv.userName = name
        return self.session

    def sign_out(self):
        try:
            if self.session.get("access_token"):
                self._req("POST", "/auth/v1/logout", {}, auth=True)
        except SyncError:
            pass
        self.session = {}
        if self.on_session:
            self.on_session({})

    def _set_session(self, res):
        self.session = {"access_token": res["access_token"], "refresh_token": res.get("refresh_token", ""),
                        "expires_at": time.time() + float(res.get("expires_in") or 3600),
                        "user": {"email": (res.get("user") or {}).get("email", self.email),
                                 "user_metadata": (res.get("user") or {}).get("user_metadata", {})}}
        if self.on_session:
            self.on_session(self.session)

    def _refresh(self) -> bool:
        rt = self.session.get("refresh_token")
        if not rt:
            return False
        code, res = self._req("POST", "/auth/v1/token?grant_type=refresh_token", {"refresh_token": rt}, auth=False)
        if code != 200 or not isinstance(res, dict) or "access_token" not in res:
            self.session = {}
            if self.on_session:
                self.on_session({})
            return False
        self._set_session(res)
        return True

    def _token(self) -> str:
        if not self.session.get("access_token"):
            if not self._refresh():
                raise AuthError("Sign in to the team server")
        elif time.time() > float(self.session.get("expires_at", 0)) - 60:
            self._refresh()
        if not self.session.get("access_token"):
            raise AuthError("Sign in to the team server")
        return self.session["access_token"]

    # ------------------------------------------------------------- data
    def _fetch(self, keys=None) -> dict:
        q = f"/rest/v1/{TABLE}?select=key,value,updated_at"
        if keys:
            q += "&key=in.(" + ",".join(urllib.parse.quote(f'"{k}"') for k in keys) + ")"
        code, rows = self._rest("GET", q)
        if code != 200 or not isinstance(rows, list):
            raise SyncError(f"Server answered {code}: {rows}")
        self.online = True
        return {r["key"]: r for r in rows}

    def _apply_values(self, values: dict):
        user = self.inv.userName
        merged = {**self.inv.dump_values(), **values}
        self.inv.load_values(merged)
        self.inv.userName = user

    def load(self):
        """Fetch everything. Unsaved local edits (from the cache) are merged in, not lost."""
        rows = self._fetch()
        theirs = {k: r["value"] for k, r in rows.items() if k in SHARED_KEYS}
        mine = self.inv.dump_values()
        pending = {k for k in SHARED_KEYS if mine.get(k) != self.base.get(k) and self.base}
        values = {}
        for k in SHARED_KEYS:
            if k in pending:
                values[k] = merge_value(k, self.base.get(k), mine.get(k), theirs.get(k), self.conflicts)
            elif k in theirs:
                values[k] = theirs[k]
        self.versions = {k: r["updated_at"] for k, r in rows.items()}
        empty = Inventory().dump_values()          # keys not on the server yet count as empty, not as edits
        self.base = {k: theirs.get(k, empty.get(k)) for k in SHARED_KEYS}
        self._apply_values(values)
        self.last_sync = time.time()
        self.last_error = ""
        self._save_cache()
        if pending:
            self.save()

    def save(self):
        """Write the collections that changed; merge with anyone who saved first. Raises SyncError when offline
        (the edits stay in memory and the cache file, and go up on the next save / check)."""
        self._save_cache()
        if not self.online and self._failed_at and time.time() - self._failed_at < 20:
            raise SyncError("Offline — saved on this PC, will sync when the server is back")
        mine = self.inv.dump_values()
        changed = [k for k in SHARED_KEYS if k in mine and mine[k] != self.base.get(k)]
        merged_any = False
        try:
            for k in changed:
                value = mine[k]
                for _attempt in range(6):
                    ver = self.versions.get(k)
                    now = _iso_now()
                    if ver is None:
                        code, res = self._rest("POST", f"/rest/v1/{TABLE}", {"key": k, "value": value,
                                                                              "updated_at": now},
                                               {"Prefer": "return=representation"})
                        if code in (200, 201) and res:
                            self._done(k, res[0])
                            break
                        if code != 409:
                            raise SyncError(f"Server answered {code}: {res}")
                    else:
                        q = (f"/rest/v1/{TABLE}?key=eq.{urllib.parse.quote(k)}"
                             f"&updated_at=eq.{urllib.parse.quote(ver)}")
                        code, res = self._rest("PATCH", q, {"value": value, "updated_at": now},
                                               {"Prefer": "return=representation"})
                        if code == 200 and res:
                            self._done(k, res[0])
                            break
                        if code not in (200, 204):
                            raise SyncError(f"Server answered {code}: {res}")
                    # someone else saved first (or the row is new): merge with theirs and try again
                    row = self._fetch([k]).get(k)
                    theirs = row["value"] if row else None
                    self.versions[k] = row["updated_at"] if row else None
                    value = merge_value(k, self.base.get(k), value, theirs, self.conflicts)
                    if row:
                        self.base[k] = theirs
                    merged_any = True
                else:
                    raise SyncError(f"Couldn't save {k}: the server kept changing — try again")
        except SyncError as e:
            self.last_error = str(e)
            if not isinstance(e, AuthError):
                self.online = False
                self._failed_at = time.time()
            raise
        self._failed_at = 0.0
        if merged_any:
            self._apply_values({k: self.base[k] for k in changed if k in self.base})
        self.last_sync = time.time()
        self.last_error = ""
        self._save_cache()
        self._notify()

    def _done(self, k, row):
        self.versions[k] = row["updated_at"]
        self.base[k] = row["value"]
        self.online = True

    def fetch_changes(self) -> dict:
        """Network only (safe on a background thread): rows changed on the server since our last sync."""
        code, rows = self._rest("GET", f"/rest/v1/{TABLE}?select=key,updated_at")
        if code != 200 or not isinstance(rows, list):
            raise SyncError(f"Server answered {code}")
        self.online = True
        remote = {r["key"]: r["updated_at"] for r in rows}
        stale = [k for k in SHARED_KEYS if k in remote and remote[k] != self.versions.get(k)]
        return self._fetch(stale) if stale else {}

    def apply_changes(self, rows: dict) -> bool:
        """UI thread: fold fetched rows into the open inventory (merging any edits not yet sent)."""
        if not rows:
            return False
        pend = set(self.pending())
        mine = self.inv.dump_values()
        values = {}
        for k, r in rows.items():
            if r["updated_at"] == self.versions.get(k):
                continue
            values[k] = merge_value(k, self.base.get(k), mine.get(k), r["value"], self.conflicts) \
                if k in pend else r["value"]
            self.base[k] = r["value"]
            self.versions[k] = r["updated_at"]
        if not values:
            return False
        self._apply_values(values)
        self.last_sync = time.time()
        self.last_error = ""
        self._save_cache()
        self._notify()
        return True

    def check_external(self) -> bool:
        """Pick up other people's changes and send edits still waiting from an offline spell (one call; the app
        runs the network part on a background thread instead)."""
        try:
            changed = self.apply_changes(self.fetch_changes())
            if self.pending():
                self.save()
                changed = True
            return changed
        except SyncError as e:
            self.last_error = str(e)
            return False

    def upload_all(self, values: dict):
        """First-time setup: put this PC's existing inventory (e.g. the local file) on the server. Merges with
        whatever is already there."""
        self._apply_values({k: v for k, v in values.items() if k in SHARED_KEYS})
        self.save()

    # ------------------------------------------------------------- cache
    def _load_cache(self):
        try:
            d = json.loads(self.cache.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if d.get("url") != self.url:
            return
        if getattr(self, "layout", "blobs") != d.get("layout", "blobs"):
            return
        self.base = d.get("base") or {}
        self.versions = d.get("versions") or {}
        self.inv.load_values(d.get("values") or {})
        self.last_sync = d.get("last_sync")
        if hasattr(self, "_load_cache_extra"):
            self._load_cache_extra(d)

    def _save_cache(self):
        try:
            self.cache.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.cache.with_suffix(".tmp")
            d = {"url": self.url, "base": self.base, "versions": self.versions,
                 "values": self.inv.dump_values(), "last_sync": self.last_sync}
            if hasattr(self, "_cache_extra"):
                d.update(self._cache_extra())
            tmp.write_text(json.dumps(d), encoding="utf-8")
            os.replace(tmp, self.cache)
        except OSError:
            pass

    def _notify(self):
        for fn in list(self.listeners):
            try:
                fn()
            except Exception:  # noqa: BLE001
                pass


def _iso_now() -> str:
    t = time.time()
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t)) + f".{int(t % 1 * 1e6):06d}+00:00"


SETUP_SQL = """-- DIMS (Daedalus Inventory Management System) team inventory: run once in Supabase ▸ SQL Editor (skip if your project already has it).
create table if not exists public.app_storage (
  key        text primary key,
  value      text,
  updated_at timestamptz not null default now()
);
alter table public.app_storage enable row level security;
drop policy if exists "team read" on public.app_storage;
drop policy if exists "team insert" on public.app_storage;
drop policy if exists "team update" on public.app_storage;
create policy "team read"   on public.app_storage for select to authenticated using (true);
create policy "team insert" on public.app_storage for insert to authenticated with check (true);
create policy "team update" on public.app_storage for update to authenticated using (true) with check (true);
-- Then add each person under Authentication ▸ Users (email + password).
"""
