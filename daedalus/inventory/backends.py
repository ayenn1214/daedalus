"""Pick the inventory store from the app settings: a local / shared-drive file, or the Supabase team server."""
from __future__ import annotations

import json

from .store import InventoryStore, get_store, set_store

K = "inventory/"


def team_config(settings) -> dict:
    return {"url": settings.value(K + "supabase_url") or "", "key": settings.value(K + "supabase_key") or ""}


def save_team_config(settings, url, key):
    settings.setValue(K + "supabase_url", url.strip())
    settings.setValue(K + "supabase_key", key.strip())


def _session(settings) -> dict:
    try:
        return json.loads(settings.value(K + "supabase_session") or "{}")
    except (TypeError, ValueError):
        return {}


def open_team(settings, url=None, key=None, layout=None):
    """layout 'rows' = the DIMS backend (supabase/migrations/*_dims.sql, one row per record);
    'blobs' = the original app_storage table (one JSON per collection)."""
    from .supabase import SupabaseStore
    from .supabase_rows import SupabaseRowStore
    cfg = team_config(settings)
    layout = layout or settings.value(K + "supabase_layout") or "rows"
    cls = SupabaseRowStore if layout == "rows" else SupabaseStore
    return cls(url or cfg["url"], key or cfg["key"], session=_session(settings),
               on_session=lambda s: settings.setValue(K + "supabase_session", json.dumps(s)))


def connect_team(settings, url, key, email, password):
    """Sign in and load, using the DIMS tables when the project has them, else the original app_storage table.
    Returns the store (layout remembered)."""
    from .supabase_rows import NoDimsTables
    save_team_config(settings, url, key)
    st = open_team(settings, url, key, layout="rows")
    st.sign_in(email, password)
    try:
        st.load()
        settings.setValue(K + "supabase_layout", "rows")
    except NoDimsTables:
        old = open_team(settings, url, key, layout="blobs")
        old.session = dict(st.session)
        old.inv.userName = st.inv.userName
        old.load()
        settings.setValue(K + "supabase_layout", "blobs")
        st = old
    return st


def setup_sql() -> str:
    """The DIMS backend migration (paste into Supabase ▸ SQL Editor, or `supabase db push`)."""
    from pathlib import Path
    for p in sorted((Path(__file__).resolve().parents[2] / "supabase" / "migrations").glob("*_dims.sql")):
        return p.read_text(encoding="utf-8")
    from .supabase import SETUP_SQL
    return SETUP_SQL


def open_store(settings, path=None):
    """The app-wide store for these settings (cached). Team mode falls back to cached data when offline."""
    if (settings.value(K + "backend") or "file") == "supabase":
        from .store import _STORE
        from .supabase import SupabaseStore, SyncError
        if isinstance(_STORE, SupabaseStore):
            return _STORE
        cfg = team_config(settings)
        if cfg["url"] and cfg["key"]:
            from .supabase_rows import NoDimsTables
            st = open_team(settings)
            if st.signed_in:
                try:
                    st.load()
                except NoDimsTables:              # project without the DIMS tables: original layout
                    settings.setValue(K + "supabase_layout", "blobs")
                    st = open_team(settings, layout="blobs")
                    try:
                        st.load()
                    except SyncError as e:
                        st.last_error = str(e)
                except SyncError as e:            # offline / expired: cached data, retried by check_external
                    st.last_error = str(e)
            set_store(st)
            return st
    return get_store(path or settings.value(K + "path") or None)


def use_file(settings, path=None):
    settings.setValue(K + "backend", "file")
    set_store(None)
    return get_store(path or settings.value(K + "path") or None)


def use_team(settings, store):
    settings.setValue(K + "backend", "supabase")
    set_store(store)
    return store
