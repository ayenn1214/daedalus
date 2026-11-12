"""DIMS team store on the Supabase backend in supabase/migrations/*_dims.sql: one row per record.

Compared with the older one-JSON-per-collection layout (supabase.py):
* only the records you changed are sent, each with its version — two people editing different parts never meet;
* stock: transactions are sent first and the database applies them to the part's stock itself, so simultaneous
  withdrawals always add up, even from accounts that may only record stock (role "floor");
* the account's role (admin / editor / floor / viewer) comes from dims_profiles; the database enforces it;
* other people's changes: only rows changed since the last check are fetched.
Sign-in, offline cache, background polling and the UI are shared with SupabaseStore.
"""
from __future__ import annotations

import copy
import json
import time
import urllib.parse

from .merge import merge_record
from .model import KEYS, num
from .supabase import AuthError, SupabaseStore, SyncError

COLLECTIONS = list(KEYS)
MRP_COLLECTIONS = {"part-numbers"}             # need the server's MRP update (schema 2)                       # storage keys == dims_records.collection values
SETTINGS = ("exchange-rate", "sync-meta")
PAGE = 1000                                    # Supabase's default max rows per request


def tx_delta(rec: dict | None) -> float:
    if not rec:
        return 0.0
    q = num(rec.get("qty"))
    return -q if rec.get("mode") == "usage" else q if rec.get("mode") == "receiving" else 0.0


def _q(v) -> str:
    return urllib.parse.quote(str(v), safe="")


class SupabaseRowStore(SupabaseStore):
    layout = "rows"

    def __init__(self, *a, **kw):
        self.rec_base: dict = {}               # (collection, id) → record as last synced
        self.rec_ver: dict = {}                # (collection, id) → version
        self.set_base: dict = {}               # setting key → value
        self.set_ver: dict = {}
        self.cursor: str | None = None         # newest updated_at seen
        self.role = ""
        self.schema = 1                        # 2 = the MRP update (part-number registry) is on the server
        super().__init__(*a, **kw)

    # ------------------------------------------------------------ display
    def label(self) -> str:
        return super().label() + (f" · {self.role}" if self.role else "")

    # ------------------------------------------------------------ local view
    def _local(self) -> dict:
        out = {}
        for coll, attr in KEYS.items():
            if coll in MRP_COLLECTIONS and self.schema < 2:
                continue                       # server without the MRP update: these stay on this PC
            for r in getattr(self.inv, attr) or []:
                if isinstance(r, dict) and r.get("id"):
                    out[(coll, str(r["id"]))] = r
        return out

    def _local_settings(self) -> dict:
        return {"exchange-rate": float(self.inv.exchangeRate or 1.35), "sync-meta": dict(self.inv.syncMeta or {})}

    def pending(self) -> list[str]:
        loc = self._local()
        keys = {k[0] for k, r in loc.items() if self.rec_base.get(k) != r}
        keys |= {k[0] for k in self.rec_base if k not in loc}
        ls = self._local_settings()
        keys |= {k for k in SETTINGS if self.set_base.get(k, _DEFAULTS[k]) != ls[k]}
        return sorted(keys)

    def _set_local(self, coll, rid, rec):
        """Replace / add / remove (rec None) one record in the open inventory, keeping list order."""
        lst = getattr(self.inv, KEYS[coll])
        for i, r in enumerate(lst):
            if isinstance(r, dict) and str(r.get("id")) == rid:
                if rec is None:
                    del lst[i]
                else:
                    lst[i] = rec
                return
        if rec is not None:
            lst.append(rec)

    # ------------------------------------------------------------ server I/O
    def _rows(self, query: str) -> list:
        out, off = [], 0
        while True:
            code, rows = self._rest("GET", f"{query}&limit={PAGE}&offset={off}")
            if code == 404:
                raise NoDimsTables("This Supabase project has no DIMS tables — run the DIMS setup SQL first")
            if code != 200 or not isinstance(rows, list):
                raise SyncError(f"Server answered {code}: {rows}")
            out += rows
            if len(rows) < PAGE:
                break
            off += PAGE
        self.online = True
        return out

    def _fetch_records(self, since: str | None = None, ids=None) -> list:
        q = "/rest/v1/dims_records?select=collection,id,data,version,deleted,updated_at&order=updated_at.asc,id.asc"
        if since:
            q += f"&updated_at=gte.{_q(since)}"
        if ids:
            coll, idl = ids
            q += f"&collection=eq.{_q(coll)}&id=in.(" + ",".join(_q(f'"{i}"') for i in idl) + ")"
        return self._rows(q)

    def _fetch_schema(self):
        code, res = self._rest("POST", "/rest/v1/rpc/dims_schema_version", {})
        self.schema = int(res) if code == 200 and isinstance(res, (int, float)) else 1

    def reserve_part_numbers(self, prefix: str, count: int, project_id=None, note: str = "") -> list[dict]:
        """Numbers handed out by the database (a lock per prefix), so two people never get the same one."""
        from .mrp import norm_prefix
        if self.schema < 2:
            raise SyncError("The team server needs the DIMS part-number update — an admin pastes the setup SQL "
                            "(Import / Export ▸ Server settings ▸ Copy DIMS setup SQL) into Supabase")
        code, res = self._rest("POST", "/rest/v1/rpc/dims_reserve_part_numbers",
                               {"p_prefix": norm_prefix(prefix), "p_count": int(count), "p_project": project_id,
                                "p_note": note or "", "p_by": self.inv.userName or self.email or "unknown"})
        self._check_write(code, res, "part numbers")
        if code != 200 or not isinstance(res, list):
            raise SyncError(f"Server answered {code}: {res}")
        out = []
        for row in res:
            k = (row["collection"], str(row["id"]))
            self.rec_ver[k] = row["version"]           # not _done_row: the fetch cursor must not skip past rows
            self.rec_base[k] = copy.deepcopy(row["data"])   # teammates wrote since this PC last fetched
            rec = copy.deepcopy(row["data"])
            self._set_local(row["collection"], str(row["id"]), rec)
            out.append(rec)
        self._save_cache()
        return out

    def _fetch_settings(self) -> list:
        return self._rows("/rest/v1/dims_settings?select=key,value,version,updated_at&order=key.asc")

    def _fetch_role(self):
        uid = (self.session.get("user") or {}).get("id")
        if not uid:
            return
        try:
            code, rows = self._rest("GET", f"/rest/v1/dims_profiles?select=role,display_name&user_id=eq.{_q(uid)}")
            if code == 200 and rows:
                self.role = rows[0].get("role") or ""
                if rows[0].get("display_name") and not self.inv.userName:
                    self.inv.userName = rows[0]["display_name"]
        except SyncError:
            pass

    def _set_session(self, res):
        super()._set_session(res)
        uid = (res.get("user") or {}).get("id")
        if uid:
            self.session["user"]["id"] = uid
            if self.on_session:
                self.on_session(self.session)

    # ------------------------------------------------------------ load
    def load(self):
        rows = self._fetch_records()
        settings = self._fetch_settings()
        self._fetch_role()
        self._fetch_schema()
        mine = self._local()
        mine_set = self._local_settings()
        had_base = bool(self.rec_base or self.set_base)
        pend = {k for k, r in mine.items() if had_base and self.rec_base.get(k) != r}
        lists = {c: [] for c in COLLECTIONS}
        server_keys = set()
        new_base, new_ver = {}, {}
        for r in rows:
            k = (r["collection"], str(r["id"]))
            new_ver[k] = r["version"]
            self._advance(r["updated_at"])
            if r["deleted"]:
                continue
            server_keys.add(k)
            new_base[k] = r["data"]
            rec = copy.deepcopy(r["data"])
            if k in pend:
                rec = merge_record(self.rec_base.get(k), mine[k], r["data"], self.conflicts)
            lists[r["collection"]].append(rec)
        for k in pend - server_keys:                       # created here while offline: keep, will be sent
            if k not in new_ver:
                lists[k[0]].append(copy.deepcopy(mine[k]))
        self.rec_base, self.rec_ver = new_base, new_ver
        for c, attr in KEYS.items():
            setattr(self.inv, attr, lists[c])
        self.set_base, self.set_ver = {}, {}
        for s in settings:
            self.set_base[s["key"]] = s["value"]
            self.set_ver[s["key"]] = s["version"]
        for key in SETTINGS:
            theirs = self.set_base.get(key, _DEFAULTS[key])
            val = mine_set[key] if had_base and mine_set[key] != self._prev_set.get(key, _DEFAULTS[key]) else theirs
            self._apply_setting(key, val)
        self._prev_set = dict(self.set_base)
        self.last_sync = time.time()
        self.last_error = ""
        self._save_cache()
        if self.pending():
            self.save()

    _prev_set: dict = {}

    def _apply_setting(self, key, val):
        if key == "exchange-rate":
            try:
                self.inv.exchangeRate = float(val) or 1.35
            except (TypeError, ValueError):
                self.inv.exchangeRate = 1.35
        else:
            self.inv.syncMeta = dict(val or {})

    def _advance(self, ts):
        if ts and (self.cursor is None or ts > self.cursor):
            self.cursor = ts

    # ------------------------------------------------------------ save
    def save(self):
        self._save_cache()
        if not self.online and self._failed_at and time.time() - self._failed_at < 20:
            raise SyncError("Offline — saved on this PC, will sync when the server is back")
        if self.role == "viewer":
            raise PermissionDenied("Your DIMS account is view-only — ask an admin to change your role")
        try:
            self._save_records()
            self._save_settings()
        except SyncError as e:
            self.last_error = str(e)
            if not isinstance(e, (AuthError, PermissionDenied)):
                self.online = False
                self._failed_at = time.time()
            raise
        self._failed_at = 0.0
        self.last_sync = time.time()
        self.last_error = ""
        self._save_cache()
        self._notify()

    def _diff(self, coll):
        mine = {k: r for k, r in self._local().items() if k[0] == coll}
        added = [k for k in mine if k not in self.rec_base]
        changed = [k for k in mine if k in self.rec_base and mine[k] != self.rec_base[k]]
        gone = [k for k in self.rec_base if k[0] == coll and k not in mine]
        return mine, added, changed, gone

    def _save_records(self):
        # 1) transactions first: the database applies each one to its part's stock
        mine, added, changed, gone = self._diff("transactions")
        stock: dict[str, float] = {}
        for k in added:
            pid = mine[k].get("partId")
            stock[pid] = stock.get(pid, 0.0) + tx_delta(mine[k])
        for k in changed:
            old, new = self.rec_base[k], mine[k]
            stock[old.get("partId")] = stock.get(old.get("partId"), 0.0) - tx_delta(old)
            stock[new.get("partId")] = stock.get(new.get("partId"), 0.0) + tx_delta(new)
        for k in gone:
            pid = self.rec_base[k].get("partId")
            stock[pid] = stock.get(pid, 0.0) - tx_delta(self.rec_base[k])
        self._insert_many("transactions", [mine[k] for k in added])
        for k in changed:
            self._write(k, mine[k])
        for k in gone:
            self._write(k, self.rec_base[k], deleted=True)
        # 2) parts whose stock the database just moved: take its numbers, keep our other edits
        moved = [pid for pid, d in stock.items() if pid and abs(d) > 1e-12 and ("parts", str(pid)) in self.rec_base]
        if moved:
            for r in self._fetch_records(ids=("parts", moved)):
                k = ("parts", str(r["id"]))
                base = copy.deepcopy(self.rec_base[k])
                base["currentStock"] = _tidy(num(base.get("currentStock")) + stock[r["id"]])
                local = self._local().get(k)
                theirs = r["data"]
                if local is None:
                    self.rec_base[k], self.rec_ver[k] = theirs, r["version"]
                    continue
                self.rec_base[k], self.rec_ver[k] = theirs, r["version"]
                merged = theirs if local == base else merge_record(base, local, theirs, self.conflicts)
                self._set_local("parts", k[1], copy.deepcopy(merged))
        # 3) everything else
        for coll in COLLECTIONS:
            if coll == "transactions":
                continue
            mine, added, changed, gone = self._diff(coll)
            self._insert_many(coll, [mine[k] for k in added])
            for k in changed:
                self._write(k, mine[k])
            for k in gone:
                self._write(k, self.rec_base[k], deleted=True)

    def _insert_many(self, coll, recs):
        if not recs:
            return
        recs = copy.deepcopy(recs)
        for i in range(0, len(recs), 500):
            chunk = recs[i:i + 500]
            body = [{"collection": coll, "id": str(r["id"]), "data": r} for r in chunk]
            code, res = self._rest("POST", "/rest/v1/dims_records", body,
                                   {"Prefer": "return=representation,resolution=ignore-duplicates"})
            self._check_write(code, res, coll)
            got = {str(r["id"]): r for r in res or []}
            for r in chunk:
                k = (coll, str(r["id"]))
                row = got.get(k[1])
                if row is not None:
                    self.rec_base[k], self.rec_ver[k] = copy.deepcopy(row["data"]), row["version"]
                    self._advance(row["updated_at"])
                else:                                       # id already on the server: merge as an edit
                    self._write(k, r)

    def _write(self, k, rec, deleted=False):
        coll, rid = k
        rec = copy.deepcopy(rec)
        for _attempt in range(6):
            ver = self.rec_ver.get(k)
            if ver is None:
                code, res = self._rest("POST", "/rest/v1/dims_records",
                                       {"collection": coll, "id": rid, "data": rec, "deleted": deleted},
                                       {"Prefer": "return=representation"})
                if code in (200, 201) and res:
                    return self._done_row(k, res[0])
                if code != 409:
                    self._check_write(code, res, coll)
            else:
                code, res = self._rest(
                    "PATCH", f"/rest/v1/dims_records?collection=eq.{_q(coll)}&id=eq.{_q(rid)}&version=eq.{ver}",
                    {"data": rec, "deleted": deleted}, {"Prefer": "return=representation"})
                self._check_write(code, res, coll)
                if res:
                    return self._done_row(k, res[0])
            rows = self._fetch_records(ids=(coll, [rid]))
            row = rows[0] if rows else None
            if row is not None and ver is not None and row["version"] == ver:
                raise PermissionDenied(f"Your DIMS account ({self.role or 'no role'}) can't change {coll}")
            if row is None:
                self.rec_ver.pop(k, None)
                continue
            theirs = None if row["deleted"] else row["data"]
            base = self.rec_base.get(k)
            self.rec_ver[k] = row["version"]
            self._advance(row["updated_at"])
            if deleted:
                if theirs is None:
                    return self._done_row(k, row)
                if base is not None and theirs != base:         # edited elsewhere since: keep their edit
                    self.conflicts.append((theirs.get("name") or rid, "deleted here, edited elsewhere — kept"))
                    self._set_local(coll, rid, copy.deepcopy(theirs))
                    return self._done_row(k, row)
                continue
            if theirs is None:                                  # deleted elsewhere, edited here: bring it back
                self.conflicts.append((rec.get("name") or rid, "deleted elsewhere, edited here — kept"))
            else:
                rec = merge_record(base, rec, theirs, self.conflicts) if base is not None else rec
            self._set_local(coll, rid, copy.deepcopy(rec))
        raise SyncError(f"Couldn't save {coll} {rid}: it kept changing on the server — try again")

    def _done_row(self, k, row):
        self.rec_ver[k] = row["version"]
        self._advance(row["updated_at"])
        if row.get("deleted"):
            self.rec_base.pop(k, None)
        else:
            self.rec_base[k] = copy.deepcopy(row["data"])
        self.online = True

    def _check_write(self, code, res, what):
        if code in (401, 403) or (isinstance(res, dict) and res.get("code") == "42501"):
            raise PermissionDenied(f"Your DIMS account ({self.role or 'no role'}) can't change {what}")
        if code not in (200, 201, 204, 409):
            raise SyncError(f"Server answered {code}: {res}")

    def _save_settings(self):
        loc = self._local_settings()
        for key in SETTINGS:
            if self.set_base.get(key, _DEFAULTS[key]) == loc[key]:
                continue
            val = loc[key]
            for _attempt in range(6):
                ver = self.set_ver.get(key)
                if ver is None:
                    code, res = self._rest("POST", "/rest/v1/dims_settings", {"key": key, "value": val},
                                           {"Prefer": "return=representation"})
                else:
                    code, res = self._rest("PATCH", f"/rest/v1/dims_settings?key=eq.{_q(key)}&version=eq.{ver}",
                                           {"value": val}, {"Prefer": "return=representation"})
                self._check_write(code, res, f"the {key} setting")
                if code in (200, 201) and res:
                    self.set_base[key], self.set_ver[key] = res[0]["value"], res[0]["version"]
                    break
                cur = {s["key"]: s for s in self._fetch_settings()}.get(key)
                if cur is not None and ver is not None and cur["version"] == ver:
                    raise PermissionDenied(f"Your DIMS account can't change the {key} setting")
                self.set_ver[key] = cur["version"] if cur else None
                if cur is not None and isinstance(val, dict) and isinstance(cur["value"], dict):
                    val = merge_record(self.set_base.get(key) or {}, val, cur["value"], self.conflicts)
                if cur is not None:
                    self.set_base[key] = cur["value"]
            else:
                raise SyncError(f"Couldn't save the {key} setting — try again")
            self._apply_setting(key, self.set_base[key])
        self._prev_set = dict(self.set_base)

    # ------------------------------------------------------------ other people's changes
    def fetch_changes(self) -> dict:
        rows = self._fetch_records(since=self.cursor) if self.cursor else self._fetch_records()
        return {"rows": rows, "settings": self._fetch_settings()}

    def apply_changes(self, payload: dict) -> bool:
        if not payload:
            return False
        changed = False
        local = self._local()
        for r in payload.get("rows", []):
            k = (r["collection"], str(r["id"]))
            self._advance(r["updated_at"])
            if self.rec_ver.get(k) is not None and r["version"] <= self.rec_ver[k]:
                continue
            mine, base = local.get(k), self.rec_base.get(k)
            pending = mine is not None and mine != base
            self.rec_ver[k] = r["version"]
            if r["deleted"]:
                self.rec_base.pop(k, None)
                if not pending:
                    self._set_local(k[0], k[1], None)
                    changed = True
                continue
            self.rec_base[k] = r["data"]
            self._set_local(k[0], k[1], copy.deepcopy(merge_record(base, mine, r["data"], self.conflicts) if pending
                                                      else r["data"]))
            changed = True
        loc = self._local_settings()
        for s in payload.get("settings", []):
            key = s["key"]
            if key not in SETTINGS or self.set_ver.get(key) == s["version"]:
                continue
            pending = loc[key] != self.set_base.get(key, _DEFAULTS[key])
            self.set_ver[key] = s["version"]
            val = s["value"]
            if pending and isinstance(val, dict) and isinstance(loc[key], dict):
                val = merge_record(self.set_base.get(key) or {}, loc[key], val, self.conflicts)
            elif pending:
                val = loc[key]
            self.set_base[key] = s["value"]
            self._apply_setting(key, val)
            changed = True
        self._prev_set = dict(self.set_base)
        if changed:
            self.last_sync = time.time()
            self.last_error = ""
            self._save_cache()
            self._notify()
        return changed

    def discard_pending(self):
        """Throw away edits the server refused: the open inventory goes back to the last synced data."""
        lists = {c: [] for c in COLLECTIONS}
        for (c, _i), rec in self.rec_base.items():
            lists[c].append(copy.deepcopy(rec))
        for c, attr in KEYS.items():
            order = {str(r.get("id")): n for n, r in enumerate(getattr(self.inv, attr) or []) if isinstance(r, dict)}
            setattr(self.inv, attr, sorted(lists[c], key=lambda r: order.get(str(r.get("id")), 1 << 30)))
        for key in SETTINGS:
            self._apply_setting(key, self.set_base.get(key, _DEFAULTS[key]))
        self._save_cache()
        self._notify()

    # ------------------------------------------------------------ admin
    def import_old_data(self) -> dict:
        """Copy the old app_storage blobs into DIMS rows (admins; records already there are left alone)."""
        code, res = self._rest("POST", "/rest/v1/rpc/dims_import_app_storage", {})
        if code != 200:
            raise SyncError((res or {}).get("message") if isinstance(res, dict) else f"Server answered {code}")
        self.load()
        return res or {}

    def status_history(self, collection: str, field: str, value) -> dict:
        """{record id: 'YYYY-MM-DD'} — the first time each record's `field` became `value`, from the audit trail
        (admins and editors can read it). Empty when the trail can't be read."""
        q = (f"/rest/v1/dims_audit?select=record_id,at,old,new&collection=eq.{_q(collection)}"
             f"&new->>{_q(field)}=eq.{_q(str(value))}&order=at.asc")
        try:
            rows = self._rows(q)
        except SyncError:
            return {}
        out = {}
        for r in rows:
            old = (r.get("old") or {}).get(field)
            if old != value and r.get("record_id") not in out:
                out[r["record_id"]] = str(r.get("at") or "")[:10]
        return out

    def people(self) -> list[dict]:
        code, rows = self._rest("GET", "/rest/v1/dims_profiles?select=user_id,email,display_name,role&order=email.asc")
        if code != 200:
            raise SyncError(f"Server answered {code}")
        return rows

    def set_role(self, user_id: str, role: str):
        if role not in ("admin", "editor", "floor", "viewer"):
            raise ValueError(role)
        code, res = self._rest("PATCH", f"/rest/v1/dims_profiles?user_id=eq.{_q(user_id)}", {"role": role},
                               {"Prefer": "return=representation"})
        if code != 200 or not res:
            raise PermissionDenied("Only a DIMS admin can change roles")

    def upload_all(self, values: dict):
        self._apply_values_rows(values)
        self.save()

    def _apply_values_rows(self, values: dict):
        """Add records from a values dict (e.g. the local file) — existing ones are merged as edits."""
        for coll, attr in KEYS.items():
            raw = values.get(coll)
            if not raw:
                continue
            for rec in json.loads(raw):
                if isinstance(rec, dict) and rec.get("id"):
                    self._set_local(coll, str(rec["id"]), rec)

    # ------------------------------------------------------------ cache
    def _cache_extra(self) -> dict:
        return {"layout": "rows", "rec_base": [[c, i, d] for (c, i), d in self.rec_base.items()],
                "rec_ver": [[c, i, v] for (c, i), v in self.rec_ver.items()], "set_base": self.set_base,
                "set_ver": self.set_ver, "cursor": self.cursor, "role": self.role}

    def _load_cache_extra(self, d: dict):
        if d.get("layout") != "rows":
            return False
        self.rec_base = {(c, i): r for c, i, r in d.get("rec_base", [])}
        self.rec_ver = {(c, i): v for c, i, v in d.get("rec_ver", [])}
        self.set_base = d.get("set_base") or {}
        self.set_ver = d.get("set_ver") or {}
        self._prev_set = dict(self.set_base)
        self.cursor = d.get("cursor")
        self.role = d.get("role") or ""
        return True


class PermissionDenied(SyncError):
    """The account's role doesn't allow that change (not an outage — don't retry)."""


class NoDimsTables(SyncError):
    """The project only has the original app_storage table."""


_DEFAULTS = {"exchange-rate": 1.35, "sync-meta": {}}


def _tidy(x):
    return int(x) if float(x).is_integer() else x
