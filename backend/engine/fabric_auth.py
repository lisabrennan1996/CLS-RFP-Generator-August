"""Shared MSAL device-code auth + Fabric SQL-endpoint connection helpers for
`fabric_daily_extract.py` (the one script that needs a live Fabric connection, run on a
schedule under an account that has workspace access to CDDA Analytics Reports_DEV).

Not used by `fabric_design_lookup.py` (kept fully self-contained as a standalone
manual/ops debugging tool -- see its own docstring) or by the app's actual runtime path
(`fabric_extract_lookup.py`, which only ever reads a local file and needs no auth at
all).

Auth: interactive/device-code sign-in via MSAL, using the same well-known first-party
Microsoft public client ID Azure CLI itself uses (no new Azure AD app registration
needed) -- token cached locally so this is a one-time prompt per machine/user, not a
per-run one.
"""
import json
import os
import sys
import time

WORKSPACE_ID = "10973c49-70db-4e4f-8f50-b72aea614c0e"
LAKEHOUSE_ID = "bfeacc6e-8c99-470a-ad52-82b3b0a7b882"

CLIENT_ID = "04b07795-8ddb-461a-bbee-02f9e1bf7b46"
AUTHORITY = "https://login.microsoftonline.com/organizations"
FABRIC_SCOPE = ["https://api.fabric.microsoft.com/.default"]
SQL_SCOPE = ["https://database.windows.net/.default"]

CACHE_DIR = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "cls-studio")
TOKEN_CACHE_PATH = os.path.join(CACHE_DIR, "fabric_token_cache.bin")

# ODBC connection-attribute key for an AAD access token (SQL_COPT_SS_ACCESS_TOKEN), and
# the struct layout pyodbc needs it packed as -- standard AAD-token-over-ODBC recipe.
SQL_COPT_SS_ACCESS_TOKEN = 1256


def emit(payload):
    print(json.dumps(payload, default=str))
    sys.exit(0)


def msal_app():
    import msal

    cache = msal.SerializableTokenCache()
    if os.path.exists(TOKEN_CACHE_PATH):
        cache.deserialize(open(TOKEN_CACHE_PATH, "r", encoding="utf-8").read())

    app = msal.PublicClientApplication(CLIENT_ID, authority=AUTHORITY, token_cache=cache)
    return app, cache


def persist_cache(cache):
    if cache.has_state_changed:
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(TOKEN_CACHE_PATH, "w", encoding="utf-8") as f:
            f.write(cache.serialize())


def _open_browser_for_signin(flow):
    opened = False
    try:
        import webbrowser

        url = flow.get("verification_uri_complete") or flow["verification_uri"]
        opened = webbrowser.open(url)
    except Exception:
        opened = False
    return opened


def get_token_blocking(app, cache, scopes, scope_key):
    """Blocking: this is only ever run unattended on a schedule (fabric_daily_extract.py),
    where there's no UI turn-taking to resume device-code polling across invocations, so
    it just blocks until the flow succeeds or expires (~15 min) instead of the
    single-poll-per-call pattern an interactive app would need."""
    for account in app.get_accounts():
        result = app.acquire_token_silent(scopes, account=account)
        if result and "access_token" in result:
            persist_cache(cache)
            return result["access_token"]

    flow = app.initiate_device_flow(scopes=scopes)
    if "user_code" not in flow:
        raise RuntimeError(f"device flow init failed: {flow}")
    print(f"[fabric_auth:{scope_key}] Sign in: {flow['verification_uri']}  code: {flow['user_code']}", file=sys.stderr)
    _open_browser_for_signin(flow)

    result = app.acquire_token_by_device_flow(flow)  # blocks until done/expired
    if not result or "access_token" not in result:
        raise RuntimeError(f"device flow token acquisition failed: {result}")
    persist_cache(cache)
    return result["access_token"]


def resolve_sql_endpoint(fabric_token):
    import urllib.request

    url = f"https://api.fabric.microsoft.com/v1/workspaces/{WORKSPACE_ID}/lakehouses/{LAKEHOUSE_ID}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {fabric_token}"})
    with urllib.request.urlopen(req) as resp:
        data = json.load(resp)
    props = data.get("properties", {}).get("sqlEndpointProperties", {})
    return props["connectionString"], props["id"]


def sql_connect(server, database, sql_token):
    import struct
    import pyodbc

    token_bytes = sql_token.encode("utf-16-le")
    token_struct = struct.pack(f"<I{len(token_bytes)}s", len(token_bytes), token_bytes)
    conn_str = (
        f"Driver={{ODBC Driver 18 for SQL Server}};Server={server};Database={database};Encrypt=yes;"
    )
    return pyodbc.connect(conn_str, attrs_before={SQL_COPT_SS_ACCESS_TOKEN: token_struct})
