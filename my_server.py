import os
import re
import inspect
import contextvars
from dotenv import load_dotenv
from supabase import create_client, Client
from fastmcp import FastMCP
import httpx
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

load_dotenv()

mcp = FastMCP("sparti-mcp")

supabase: Client = create_client(
    os.environ["SUPABASE_URL"],
    os.environ["SUPABASE_KEY"],
)

# Fallback to env var; per-request key (from Bearer token) takes priority via _request_composio_key.
COMPOSIO_API_KEY = os.getenv("COMPOSIO_API_KEY", "")
COMPOSIO_V2 = "https://backend.composio.dev/api/v2"
COMPOSIO_V3 = "https://backend.composio.dev/api/v3"

# Per-request context vars set by middleware from incoming headers.
_request_composio_key: contextvars.ContextVar[str] = contextvars.ContextVar(
    "_request_composio_key", default=""
)
_request_user_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "_request_user_id", default=""
)

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE
)


class ComposioKeyMiddleware(BaseHTTPMiddleware):
    """Extracts Bearer token → COMPOSIO_API_KEY ContextVar for the request."""

    async def dispatch(self, request: Request, call_next: object):
        auth = request.headers.get("authorization", "")
        key = auth.removeprefix("Bearer ").strip() if auth.startswith("Bearer ") else ""
        token = _request_composio_key.set(key) if key else None
        try:
            return await call_next(request)
        finally:
            if token is not None:
                _request_composio_key.reset(token)


class UserIdMiddleware(BaseHTTPMiddleware):
    """Extracts X-Sparti-User-Id header → user_id ContextVar for the request.
    The mcp-proxy edge function injects this header from the verified JWT."""

    async def dispatch(self, request: Request, call_next: object):
        user_id = request.headers.get("x-sparti-user-id", "")
        token = _request_user_id.set(user_id) if user_id else None
        try:
            return await call_next(request)
        finally:
            if token is not None:
                _request_user_id.reset(token)


def _effective_composio_key() -> str:
    return _request_composio_key.get() or COMPOSIO_API_KEY


def _composio_headers() -> dict:
    # Per-request key (from mcp-proxy Bearer) takes priority over the env-var fallback.
    return {"x-api-key": _effective_composio_key(), "Content-Type": "application/json"}


# ── Supabase tools ────────────────────────────────────────────────────────────

@mcp.tool
def greet(name: str) -> str:
    """Greet someone."""
    return f"Hello, {name}!"


@mcp.tool
def query_table(table: str, limit: int = 10, filters: dict | None = None) -> list:
    """Fetch rows from any Sparti Supabase table. Optionally pass filters as {column: value}."""
    q = supabase.table(table).select("*").limit(limit)
    if filters:
        for col, val in filters.items():
            q = q.eq(col, val)
    return q.execute().data


@mcp.tool
def insert_row(table: str, data: dict) -> list:
    """Insert a row into a Sparti table. Returns the inserted record."""
    return supabase.table(table).insert(data).execute().data


@mcp.tool
def update_rows(table: str, filters: dict, updates: dict) -> list:
    """Update rows matching filters in a Sparti table. Returns updated records."""
    q = supabase.table(table).update(updates)
    for col, val in filters.items():
        q = q.eq(col, val)
    return q.execute().data


# ── Composio tools ────────────────────────────────────────────────────────────

@mcp.tool
async def list_composio_tools(apps: list[str], limit: int = 20) -> list:
    """List available Composio tools for given app slugs (e.g. SLACK, GMAIL, CLICKUP).
    Returns tool names and descriptions."""
    if not _effective_composio_key():
        return [{"error": "COMPOSIO_API_KEY not configured"}]
    params = {"apps": ",".join(apps), "limit": limit}
    async with httpx.AsyncClient() as client:
        res = await client.get(
            f"{COMPOSIO_V2}/actions",
            params=params,
            headers=_composio_headers(),
            timeout=15,
        )
        res.raise_for_status()
        data = res.json()
    items = data.get("items", [])
    return [{"name": t["name"], "description": t.get("description", "")} for t in items]


@mcp.tool
async def execute_composio_tool(
    tool_name: str,
    params: dict,
    entity_id: str = "default",
) -> dict:
    """Execute a Composio action by slug (e.g. SLACK_SEND_MESSAGE, GMAIL_SEND_EMAIL).
    entity_id identifies the connected user account (default: 'default').
    For Sparti chat, pass the active brand id as entity_id to use brand-scoped credentials,
    or 'default' for account-level."""
    if not _effective_composio_key():
        return {"error": "COMPOSIO_API_KEY not configured"}
    body = {"input": params, "entityId": entity_id}
    async with httpx.AsyncClient() as client:
        res = await client.post(
            f"{COMPOSIO_V3}/actions/{tool_name}/execute",
            json=body,
            headers=_composio_headers(),
            timeout=30,
        )
        res.raise_for_status()
        return res.json()


async def _composio_get(path: str, params: dict | None = None) -> dict:
    """Internal: GET against Composio v3 with API-key headers."""
    async with httpx.AsyncClient() as client:
        res = await client.get(
            f"{COMPOSIO_V3}{path}",
            params=params or {},
            headers=_composio_headers(),
            timeout=15,
        )
        res.raise_for_status()
        return res.json()


async def _resolve_auth_config_id(toolkit: str) -> str | None:
    """Find the first ENABLED auth_config for a toolkit slug (e.g. 'gmail', 'google_calendar').
    Fetches all configs and filters client-side — the Composio v3 `toolkit` query param
    is not reliably applied and would otherwise return every config (wrong first pick)."""
    slug = toolkit.lower().replace(" ", "_")
    data = await _composio_get("/auth_configs", {"limit": 100})
    items = data.get("items") or data.get("auth_configs") or []

    def _item_slug(cfg: dict) -> str:
        raw = (
            (cfg.get("toolkit") or {}).get("slug")
            or cfg.get("toolkit_slug")
            or cfg.get("appName")
            or cfg.get("app_name")
            or cfg.get("name")
            or ""
        )
        return raw.lower().replace("-", "_").replace(" ", "_")

    matching = [cfg for cfg in items if _item_slug(cfg) == slug]
    if not matching:
        # Fallback: strip underscores so google_calendar matches googlecalendar, etc.
        slug_flat = slug.replace("_", "")
        matching = [cfg for cfg in items if _item_slug(cfg).replace("_", "") == slug_flat]

    for cfg in matching:
        if cfg.get("status") == "ENABLED" or cfg.get("enabled") is True:
            return cfg.get("id")
    return matching[0].get("id") if matching else None


@mcp.tool
async def list_composio_connections(entity_id: str | None = None, limit: int = 50) -> list:
    """List existing Composio connections.
    If entity_id is provided, filter to that entity (typically the active brand id).
    Returns a list of {id, toolkit, status, display_name, entity_id}."""
    if not _effective_composio_key():
        return [{"error": "COMPOSIO_API_KEY not configured"}]
    params: dict = {"limit": limit}
    if entity_id:
        params["user_ids"] = entity_id
    data = await _composio_get("/connected_accounts", params)
    items = data.get("items") or []
    return [
        {
            "id": it.get("id"),
            "toolkit": (it.get("toolkit") or {}).get("slug")
            or it.get("toolkit_slug")
            or it.get("appName"),
            "status": it.get("status"),
            "display_name": it.get("display_name") or it.get("displayName"),
            "entity_id": it.get("user_id") or it.get("userId") or it.get("entity_id"),
        }
        for it in items
    ]


@mcp.tool
async def connect_composio_app(
    toolkit: str,
    entity_id: str = "default",
    callback_url: str | None = None,
) -> dict:
    """Start an OAuth flow to connect a Composio toolkit (Gmail, Slack, etc.) for an entity.

    `entity_id` scopes the connection — pass the active brand id for brand-level access,
    or 'default' for account-level. `toolkit` is the Composio slug (e.g. 'gmail', 'slack').

    Returns {redirect_url, connection_id, status, toolkit, entity_id}. Present `redirect_url`
    to the user as a clickable link — they finish OAuth in their browser. Once authorized,
    the connection becomes ACTIVE and is callable via `execute_composio_tool`."""
    if not _effective_composio_key():
        return {"error": "COMPOSIO_API_KEY not configured"}

    auth_config_id = await _resolve_auth_config_id(toolkit)
    if not auth_config_id:
        return {
            "error": f"No auth_config for toolkit '{toolkit}'. "
            "Configure it in the Sparti Integrations page before connecting from chat."
        }

    # No callback_url by default — Composio shows its built-in success page.
    # The user closes that tab themselves and tells chat "done", which calls
    # save_composio_connection to verify + persist the connection.
    body: dict = {
        "auth_config": {"id": auth_config_id},
        "connection": {
            "user_id": entity_id,
            **({"callback_url": callback_url} if callback_url else {}),
            "extra_params": {"prompt": "select_account"},
        },
        "force_new_integration": True,
    }

    async with httpx.AsyncClient() as client:
        res = await client.post(
            f"{COMPOSIO_V3}/connected_accounts",
            json=body,
            headers=_composio_headers(),
            timeout=30,
        )
        json_body = res.json() if res.content else {}

        # Fall back to /link when the main endpoint refuses or silently reuses an old connection.
        redirect = (
            json_body.get("redirectUrl")
            or json_body.get("redirect_url")
            or (json_body.get("connectionData") or {}).get("redirectUrl")
            or ((json_body.get("connectionData") or {}).get("val") or {}).get("redirectUrl")
            or ((json_body.get("connectionData") or {}).get("val") or {}).get("authUri")
        )
        needs_fallback = not res.is_success or (
            res.is_success and not redirect and json_body.get("status") == "ACTIVE"
        )
        if needs_fallback:
            flat = {
                "auth_config_id": auth_config_id,
                "user_id": entity_id,
                **({"callback_url": callback_url} if callback_url else {}),
                "force_new_integration": True,
            }
            res = await client.post(
                f"{COMPOSIO_V3}/connected_accounts/link",
                json=flat,
                headers=_composio_headers(),
                timeout=30,
            )
            json_body = res.json() if res.content else {}
            redirect = (
                json_body.get("redirectUrl")
                or json_body.get("redirect_url")
                or (json_body.get("connectionData") or {}).get("redirectUrl")
                or ((json_body.get("connectionData") or {}).get("val") or {}).get("redirectUrl")
                or ((json_body.get("connectionData") or {}).get("val") or {}).get("authUri")
            )

    if not res.is_success:
        msg = (
            (json_body.get("error") or {}).get("message")
            if isinstance(json_body.get("error"), dict)
            else None
        ) or json_body.get("message") or json_body.get("detail") or f"Composio refused (status {res.status_code})"
        return {"error": str(msg)}

    return {
        "redirect_url": redirect,
        "connection_id": json_body.get("id") or json_body.get("connectionId"),
        "status": json_body.get("status"),
        "toolkit": toolkit,
        "entity_id": entity_id,
    }


@mcp.tool
async def save_composio_connection(
    connection_id: str,
    toolkit_slug: str,
    entity_id: str = "default",
) -> dict:
    """Verify a Composio connection is ACTIVE and persist it to the Supabase
    composio_connections table so it appears on the Integrations page.
    Call this AFTER the user confirms they completed the OAuth authorization.
    entity_id is the brand id (UUID) or 'default' for account-level."""
    user_id = _request_user_id.get()
    if not user_id:
        return {"error": "No user identity available — save skipped. Ask the user to refresh and try again."}
    if not _effective_composio_key():
        return {"error": "COMPOSIO_API_KEY not configured"}

    # Verify the connection is ACTIVE in Composio.
    try:
        data = await _composio_get(f"/connected_accounts/{connection_id}")
        status = data.get("status", "")
        if status != "ACTIVE":
            return {
                "error": f"Connection is not yet ACTIVE (status={status!r}). "
                "Please complete the OAuth authorization first, then try again."
            }
        resolved_slug = (
            (data.get("toolkit") or {}).get("slug") or toolkit_slug
        ).lower()
        display_name = data.get("display_name") or data.get("displayName") or None
        auth_config_id = (data.get("authConfig") or {}).get("id") or data.get("auth_config_id") or None
    except Exception as e:
        return {"error": f"Could not verify connection with Composio: {e}"}

    # Resolve brand_id: valid UUID → brand scope; anything else → account scope (null).
    brand_id = entity_id if (entity_id and entity_id != "default" and _UUID_RE.match(entity_id)) else None

    # Persist to Supabase (service-role client bypasses RLS).
    try:
        row: dict = {
            "user_id": user_id,
            "toolkit_slug": resolved_slug,
            "connection_id": connection_id,
            "status": "ACTIVE",
        }
        if display_name:
            row["display_name"] = display_name
        if auth_config_id:
            row["auth_config_id"] = auth_config_id
        if brand_id:
            row["brand_id"] = brand_id

        supabase.table("composio_connections").upsert(
            row, on_conflict="user_id,toolkit_slug,connection_id"
        ).execute()
    except Exception as e:
        return {"error": f"Composio connection verified but DB save failed: {e}"}

    return {
        "ok": True,
        "connection_id": connection_id,
        "toolkit": resolved_slug,
        "brand_id": brand_id,
        "display_name": display_name,
    }


@mcp.tool
async def disconnect_composio_connection(connection_id: str) -> dict:
    """Delete a Composio connection by id. Returns {ok, connection_id}."""
    if not _effective_composio_key():
        return {"error": "COMPOSIO_API_KEY not configured"}
    async with httpx.AsyncClient() as client:
        res = await client.delete(
            f"{COMPOSIO_V3}/connected_accounts/{connection_id}",
            headers=_composio_headers(),
            timeout=15,
        )
    if not res.is_success:
        return {"error": f"Composio refused (status {res.status_code})", "connection_id": connection_id}
    return {"ok": True, "connection_id": connection_id}


# ── REST bridge for Supabase mcp-proxy edge function ─────────────────────────

@mcp.custom_route("/api/tools", methods=["GET"])
async def api_list_tools(request: Request) -> Response:
    """Return all registered MCP tools as JSON.
    Called by the Supabase mcp-proxy edge function."""
    tools = mcp._tool_manager._tools
    return JSONResponse({
        "tools": [
            {
                "name": name,
                "description": tool.description or "",
                "parameters": tool.parameters,
            }
            for name, tool in tools.items()
        ]
    })


@mcp.custom_route("/api/execute", methods=["POST"])
async def api_execute_tool(request: Request) -> Response:
    """Execute an MCP tool by name.
    Body: {"tool": str, "params": dict}
    Called by the Supabase mcp-proxy edge function."""
    body = await request.json()
    tool_name = body.get("tool")
    params = body.get("params", {})

    if not tool_name:
        return JSONResponse({"error": "Missing 'tool' field"}, status_code=400)

    tools = mcp._tool_manager._tools
    if tool_name not in tools:
        return JSONResponse({"error": f"Unknown tool: {tool_name}"}, status_code=404)

    fn = tools[tool_name].fn
    try:
        result = await fn(**params) if inspect.iscoroutinefunction(fn) else fn(**params)
        return JSONResponse({"result": result})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# ── ASGI app (with middleware) — used by uvicorn entrypoint ───────────────────

app = mcp.http_app()
app.add_middleware(UserIdMiddleware)
app.add_middleware(ComposioKeyMiddleware)
