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


def open_team(settings, url=None, key=None):
    from .supabase import SupabaseStore
    cfg = team_config(settings)
    st = SupabaseStore(url or cfg["url"], key or cfg["key"], session=_session(settings),
                       on_session=lambda s: settings.setValue(K + "supabase_session", json.dumps(s)))
    return st


def open_store(settings, path=None):
    """The app-wide store for these settings (cached). Team mode falls back to cached data when offline."""
    if (settings.value(K + "backend") or "file") == "supabase":
        from .store import _STORE
        from .supabase import SupabaseStore, SyncError
        if isinstance(_STORE, SupabaseStore):
            return _STORE
        cfg = team_config(settings)
        if cfg["url"] and cfg["key"]:
            st = open_team(settings)
            if st.signed_in:
                try:
                    st.load()
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
