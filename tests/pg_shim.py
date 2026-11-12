"""Tests only: a tiny stand-in for Supabase's REST (PostgREST) and auth (GoTrue) APIs in front of a REAL Postgres
database with the DIMS migration applied — so the desktop client is tested against the real triggers, versions
and row-level security. Covers just the requests the DIMS client makes."""
from __future__ import annotations

import datetime as dt
import json
import re
import secrets
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

ANON = "anon-test-key"
TABLES = {"dims_records", "dims_settings", "dims_profiles", "dims_audit", "dims_parts_report"}


def _enc(v):
    if isinstance(v, dt.datetime):
        return v.astimezone(dt.timezone.utc).isoformat()
    if hasattr(v, "hex") and v.__class__.__name__ == "UUID":
        return str(v)
    raise TypeError(type(v))


class Shim:
    def __init__(self, dsn: str):
        self.dsn = dsn
        self.passwords: dict[str, str] = {}
        self.tokens: dict[str, tuple[str, float]] = {}
        self.refresh: dict[str, str] = {}
        self.down = False
        self.requests = 0
        shim = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, obj=None):
                body = b"" if obj is None else json.dumps(obj, default=_enc).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _body(self):
                n = int(self.headers.get("Content-Length") or 0)
                return json.loads(self.rfile.read(n) or b"null")

            def _handle(self, method):
                shim.requests += 1
                if shim.down:
                    return self._send(503, {"message": "down"})
                if self.headers.get("apikey") != ANON:
                    return self._send(401, {"message": "Invalid API key"})
                u = urllib.parse.urlparse(self.path)
                if u.path.startswith("/auth/v1/"):
                    return self._auth(u)
                tok = (self.headers.get("Authorization") or "").removeprefix("Bearer ")
                uid, exp = shim.tokens.get(tok, (None, 0))
                if not uid or exp < time.time():
                    return self._send(401, {"code": "PGRST301", "message": "JWT expired"})
                try:
                    with psycopg.connect(shim.dsn, autocommit=False) as con:
                        con.execute("set time zone 'UTC'")
                        con.execute("set local role authenticated")
                        con.execute("select set_config('request.jwt.claims', %s, true)",
                                    (json.dumps({"sub": uid, "role": "authenticated"}),))
                        code, out = shim.rest(con, method, u, self._body() if method != "GET" else None,
                                              self.headers.get("Prefer") or "")
                        con.commit()
                    return self._send(code, out)
                except psycopg.errors.InsufficientPrivilege as e:
                    return self._send(403, {"code": "42501", "message": str(e).splitlines()[0]})
                except psycopg.errors.UniqueViolation as e:
                    return self._send(409, {"code": "23505", "message": str(e).splitlines()[0]})
                except psycopg.errors.UndefinedTable as e:
                    return self._send(404, {"code": "PGRST205", "message": str(e).splitlines()[0]})
                except psycopg.errors.RaiseException as e:
                    return self._send(400, {"code": "P0001", "message": str(e).splitlines()[0]})

            def _auth(self, u):
                q = urllib.parse.parse_qs(u.query)
                b = self._body() or {}
                if u.path == "/auth/v1/logout":
                    return self._send(204)
                grant = q.get("grant_type", [""])[0]
                if grant == "password":
                    if shim.passwords.get(b.get("email")) != b.get("password"):
                        return self._send(400, {"error_description": "Invalid login credentials"})
                    email = b["email"]
                elif grant == "refresh_token":
                    email = shim.refresh.pop(b.get("refresh_token"), None)
                    if not email:
                        return self._send(400, {"error_description": "Invalid Refresh Token"})
                else:
                    return self._send(400, {"error_description": "unsupported grant"})
                with psycopg.connect(shim.dsn) as con:
                    uid = str(con.execute("select id from auth.users where email = %s", (email,)).fetchone()[0])
                at, rt = secrets.token_hex(8), secrets.token_hex(8)
                shim.tokens[at] = (uid, time.time() + 3600)
                shim.refresh[rt] = email
                return self._send(200, {"access_token": at, "refresh_token": rt, "expires_in": 3600,
                                        "user": {"id": uid, "email": email, "user_metadata": {}}})

            def do_GET(self):  # noqa: N802
                self._handle("GET")

            def do_POST(self):  # noqa: N802
                self._handle("POST")

            def do_PATCH(self):  # noqa: N802
                self._handle("PATCH")

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def stop(self):
        self.httpd.shutdown()

    def add_user(self, email, password):
        with psycopg.connect(self.dsn, autocommit=True) as con:
            uid = con.execute("insert into auth.users (email) values (%s) returning id", (email,)).fetchone()[0]
        self.passwords[email] = password
        return str(uid)

    # ------------------------------------------------------------------ PostgREST subset
    @staticmethod
    def _where(q):
        parts, args = [], []
        for col, vals in q.items():
            if col in ("select", "order", "limit", "offset"):
                continue
            v = vals[0]
            op, _, val = v.partition(".")
            ident = sql.Identifier(col)
            if "->>" in col:                                    # json path: new->>status=eq.Complete
                c, _, key = col.partition("->>")
                if op != "eq":
                    raise ValueError(op)
                parts.append(sql.SQL("{}->>%s = %s").format(sql.Identifier(c)))
                args += [key, val]
                continue
            if op == "eq":
                parts.append(sql.SQL("{}::text = %s").format(ident) if col != "updated_at"
                             else sql.SQL("{} = %s::timestamptz").format(ident))
                args.append(val)
            elif op in ("gte", "gt"):
                parts.append(sql.SQL("{} " + (">=" if op == "gte" else ">") + " %s").format(ident))
                args.append(val)
            elif op == "in":
                items = [x.strip().strip('"') for x in val.strip("()").split(",") if x.strip()]
                parts.append(sql.SQL("{}::text = any(%s)").format(ident))
                args.append(items)
            else:
                raise ValueError(op)
        return (sql.SQL(" where ") + sql.SQL(" and ").join(parts) if parts else sql.SQL("")), args

    def rest(self, con, method, u, body, prefer):
        path = u.path.removeprefix("/rest/v1/")
        q = urllib.parse.parse_qs(u.query, keep_blank_values=True)
        if path.startswith("rpc/"):
            fn = path[4:]
            row = con.execute(sql.SQL("select {}()").format(sql.Identifier(fn))).fetchone()
            return 200, row[0]
        if path not in TABLES:
            con.execute(sql.SQL("select * from {}").format(sql.Identifier(path)))
        table = sql.Identifier(path)
        where, args = self._where(q)
        if method == "GET":
            cols = q.get("select", ["*"])[0]
            colsql = sql.SQL("*") if cols == "*" else sql.SQL(", ").join(sql.Identifier(c) for c in cols.split(","))
            order = sql.SQL("")
            if "order" in q:
                order = sql.SQL(" order by ") + sql.SQL(", ").join(
                    sql.SQL("{} " + ("desc" if o.endswith(".desc") else "asc")).format(sql.Identifier(o.split(".")[0]))
                    for o in q["order"][0].split(","))
            lim = sql.SQL(" limit {} offset {}").format(sql.Literal(int(q.get("limit", ["1000"])[0])),
                                                      sql.Literal(int(q.get("offset", ["0"])[0])))
            cur = con.cursor(row_factory=psycopg.rows.dict_row)
            rows = cur.execute(sql.SQL("select {} from {}").format(colsql, table) + where + order + lim, args).fetchall()
            return 200, rows
        if method == "POST":
            recs = body if isinstance(body, list) else [body]
            out = []
            ignore = "ignore-duplicates" in prefer
            cur = con.cursor(row_factory=psycopg.rows.dict_row)
            for r in recs:
                cols = list(r)
                vals = [Jsonb(r[c]) if isinstance(r[c], (dict, list)) or c in ("data", "value") else r[c] for c in cols]
                stmt = sql.SQL("insert into {} ({}) values ({})").format(
                    table, sql.SQL(", ").join(sql.Identifier(c) for c in cols),
                    sql.SQL(", ").join(sql.Placeholder() * len(cols)))
                if ignore:
                    stmt += sql.SQL(" on conflict do nothing")
                stmt += sql.SQL(" returning *")
                row = cur.execute(stmt, vals).fetchone()
                if row:
                    out.append(row)
            return 201, out
        if method == "PATCH":
            sets = sql.SQL(", ").join(sql.SQL("{} = %s").format(sql.Identifier(c)) for c in body)
            vals = [Jsonb(v) if isinstance(v, (dict, list)) or c in ("data", "value") else v for c, v in body.items()]
            cur = con.cursor(row_factory=psycopg.rows.dict_row)
            rows = cur.execute(sql.SQL("update {} set ").format(table) + sets + where + sql.SQL(" returning *"),
                               vals + args).fetchall()
            return 200, rows
        return 405, {"message": "method"}


def ready_db(admin_dsn: str, name: str, migration: str, stub: str) -> str:
    """Fresh database with the Supabase stub and the DIMS migration; returns its DSN."""
    with psycopg.connect(admin_dsn, autocommit=True) as con:
        con.execute(sql.SQL("drop database if exists {}").format(sql.Identifier(name)))
        con.execute(sql.SQL("create database {}").format(sql.Identifier(name)))
    dsn = re.sub(r"dbname=\w+", f"dbname={name}", admin_dsn)
    with psycopg.connect(dsn, autocommit=True) as con:
        con.execute(open(stub).read())
        con.execute(open(migration).read())
    return dsn
