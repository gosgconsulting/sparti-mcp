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
_request_user_jwt: contextvars.ContextVar[str] = contextvars.ContextVar(
    "_request_user_jwt", default=""
)

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE
)

# Hard allowlist of tables the generic `query_table` / `insert_row` / `update_rows`
# tools are allowed to touch. Anything not in this list is rejected — protects
# against prompt-injection abuse asking the LLM to read auth.users / api_keys / etc.
# High-level domain tools (update_workflow, save_composio_connection) are NOT
# bound by this list — they're per-table by construction.
TABLE_ALLOWLIST: set[str] = {
    "ai_workflows",
    "ai_workflow_steps",
    "ai_workflow_inputs",
    "ai_workflow_runs",
    "learning_workflows",
    "learning_workflow_steps",
    "agent_jobs",
    "agent_tool_calls",
    "composio_connections",
    "brands",
    "brand_briefs",
}


def _check_table_allowed(table: str) -> dict | None:
    """Return an error dict if the table isn't in TABLE_ALLOWLIST, else None."""
    if table not in TABLE_ALLOWLIST:
        return {
            "error": f"Table '{table}' is not allowed via generic ops. "
            f"Allowed: {sorted(TABLE_ALLOWLIST)}. Ask for a specific tool if you need a different table."
        }
    return None


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
    """Extracts X-Sparti-User-Id and X-Sparti-User-Jwt headers into
    per-request ContextVars. The mcp-proxy edge function injects both
    after verifying the JWT, so MCP tools can act as the authenticated
    user against Supabase (RLS-aware)."""

    async def dispatch(self, request: Request, call_next: object):
        user_id = request.headers.get("x-sparti-user-id", "")
        user_jwt = request.headers.get("x-sparti-user-jwt", "")
        id_token = _request_user_id.set(user_id) if user_id else None
        jwt_token = _request_user_jwt.set(user_jwt) if user_jwt else None
        try:
            return await call_next(request)
        finally:
            if id_token is not None:
                _request_user_id.reset(id_token)
            if jwt_token is not None:
                _request_user_jwt.reset(jwt_token)


def _user_supabase() -> Client:
    """Return a Supabase client authenticated as the calling user (RLS-aware).
    Falls back to the static anon-key client when no JWT is in context."""
    jwt = _request_user_jwt.get()
    if not jwt:
        return supabase
    client = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])
    # postgrest.auth() sets the Authorization header on PostgREST calls; RLS
    # then sees auth.uid() = user_id and the policy allows the operation.
    client.postgrest.auth(jwt)
    return client


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
def query_table(table: str, limit: int = 10, filters: dict | None = None) -> list | dict:
    """Read rows from an allow-listed Sparti Supabase table (RLS-aware — returns
    only rows the calling user is permitted to see). Pass filters as {column: value}.
    Allowed tables: see TABLE_ALLOWLIST in the server source."""
    err = _check_table_allowed(table)
    if err:
        return [err]
    q = _user_supabase().table(table).select("*").limit(limit)
    if filters:
        for col, val in filters.items():
            q = q.eq(col, val)
    return q.execute().data


@mcp.tool
def insert_row(table: str, data: dict) -> list | dict:
    """Insert a row into an allow-listed Sparti table (RLS-aware). Returns the
    inserted record, or an error dict on policy/allowlist failure."""
    err = _check_table_allowed(table)
    if err:
        return [err]
    return _user_supabase().table(table).insert(data).execute().data


@mcp.tool
def update_rows(table: str, filters: dict, updates: dict) -> list | dict:
    """Update rows matching filters in an allow-listed Sparti table (RLS-aware).
    Returns updated records, or an error dict on policy/allowlist failure."""
    err = _check_table_allowed(table)
    if err:
        return [err]
    q = _user_supabase().table(table).update(updates)
    for col, val in filters.items():
        q = q.eq(col, val)
    return q.execute().data


# ── Workflow / agent data ops (high-level, RLS-aware) ─────────────────────────

@mcp.tool
def list_workflows(brand_id: str | None = None, limit: int = 50) -> list:
    """List ai_workflows visible to the current user (optionally filtered by brand).
    Returns id, name, description, brand_id, status, updated_at."""
    q = (
        _user_supabase()
        .table("ai_workflows")
        .select("id, name, description, brand_id, status, updated_at")
        .order("updated_at", desc=True)
        .limit(limit)
    )
    if brand_id:
        q = q.eq("brand_id", brand_id)
    return q.execute().data


@mcp.tool
def get_workflow(workflow_id: str) -> dict:
    """Get a single workflow with its steps and inputs."""
    sb = _user_supabase()
    wf = sb.table("ai_workflows").select("*").eq("id", workflow_id).maybeSingle().execute().data
    if not wf:
        return {"error": f"Workflow {workflow_id} not found or not visible to you"}
    steps = sb.table("ai_workflow_steps").select("*").eq("workflow_id", workflow_id).order("position").execute().data
    inputs = sb.table("ai_workflow_inputs").select("*").eq("workflow_id", workflow_id).execute().data
    return {"workflow": wf, "steps": steps or [], "inputs": inputs or []}


@mcp.tool
def update_workflow(workflow_id: str, updates: dict) -> dict:
    """Update an ai_workflows row. `updates` is a partial object — only fields
    you want to change. Returns the updated row, or an error if RLS blocks it.
    Common fields: name, description, status."""
    res = (
        _user_supabase()
        .table("ai_workflows")
        .update(updates)
        .eq("id", workflow_id)
        .execute()
    )
    if not res.data:
        return {"error": f"Workflow {workflow_id} update failed — not found or RLS denied"}
    return {"ok": True, "workflow": res.data[0]}


@mcp.tool
def update_workflow_step(step_id: str, updates: dict) -> dict:
    """Update an ai_workflow_steps row by id. `updates` is a partial object.
    Common fields: name, prompt, model, position, config."""
    res = (
        _user_supabase()
        .table("ai_workflow_steps")
        .update(updates)
        .eq("id", step_id)
        .execute()
    )
    if not res.data:
        return {"error": f"Workflow step {step_id} update failed — not found or RLS denied"}
    return {"ok": True, "step": res.data[0]}


@mcp.tool
def list_agent_jobs(status: str | None = None, limit: int = 50) -> list:
    """List recent rows from agent_jobs (RLS-aware). Filter by status if given
    (e.g. 'pending', 'running', 'completed', 'failed')."""
    q = (
        _user_supabase()
        .table("agent_jobs")
        .select("*")
        .order("created_at", desc=True)
        .limit(limit)
    )
    if status:
        q = q.eq("status", status)
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

        # Use a JWT-authenticated client so RLS sees the request as the user
        # and allows the insert (auth.uid() = user_id policy).
        _user_supabase().table("composio_connections").upsert(
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
    """Delete a Composio connection by id. Also marks the local
    composio_connections row as INACTIVE so the Integrations page reflects it.
    Returns {ok, connection_id}."""
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

    # Best-effort local mirror — don't fail the disconnect if RLS blocks the update.
    try:
        _user_supabase().table("composio_connections").update(
            {"status": "INACTIVE"}
        ).eq("connection_id", connection_id).execute()
    except Exception as e:  # pragma: no cover — log but don't surface
        print(f"[disconnect] local mirror update failed: {e}")

    return {"ok": True, "connection_id": connection_id}


# ── Composio parity tools (replacing composio-proxy edge fn) ─────────────────

@mcp.tool
async def list_composio_auth_configs(toolkit: str | None = None, limit: int = 100) -> list:
    """List Composio auth_configs available on the workspace. Filter to a single
    toolkit slug (e.g. 'gmail') by passing it as `toolkit`. Each item has
    {id, toolkit_slug, name, status, is_composio_managed}."""
    if not _effective_composio_key():
        return [{"error": "COMPOSIO_API_KEY not configured"}]
    data = await _composio_get("/auth_configs", {"limit": limit})
    items = data.get("items") or data.get("auth_configs") or []

    out = []
    for cfg in items:
        slug = (
            (cfg.get("toolkit") or {}).get("slug")
            or cfg.get("toolkit_slug")
            or cfg.get("appName")
            or cfg.get("app_name")
            or ""
        ).lower()
        if toolkit and slug != toolkit.lower().replace(" ", "_"):
            continue
        out.append({
            "id": cfg.get("id"),
            "toolkit_slug": slug,
            "name": cfg.get("name") or cfg.get("display_name"),
            "status": cfg.get("status"),
            "is_composio_managed": cfg.get("is_composio_managed") or cfg.get("isComposioManaged"),
        })
    return out


@mcp.tool
async def list_composio_toolkits(category: str | None = None, limit: int = 200) -> list:
    """List all Composio toolkits (apps) available for connection — what the
    Integrations connector picker shows. Filter by category (e.g. 'productivity',
    'ai') if provided. Each item: {slug, name, description, logo, category}."""
    if not _effective_composio_key():
        return [{"error": "COMPOSIO_API_KEY not configured"}]
    params: dict = {"limit": limit}
    if category:
        params["category"] = category
    data = await _composio_get("/toolkits", params)
    items = data.get("items") or data.get("toolkits") or []
    return [
        {
            "slug": it.get("slug") or it.get("appName") or it.get("name", "").lower(),
            "name": it.get("name") or it.get("display_name"),
            "description": it.get("description") or it.get("meta", {}).get("description"),
            "logo": it.get("logo") or it.get("meta", {}).get("logo"),
            "category": (it.get("categories") or [None])[0] or it.get("category"),
        }
        for it in items
    ]


@mcp.tool
async def get_composio_tool_schemas(toolkits: list[str], limit: int = 30) -> list:
    """Return OpenAI-shaped tool schemas for the given toolkit slugs — what the
    chat injects into the LLM tool list. Pass only **connected** toolkits.
    Each item: {type:'function', function:{name, description, parameters}}."""
    if not _effective_composio_key():
        return [{"error": "COMPOSIO_API_KEY not configured"}]
    if not toolkits:
        return []
    apps = ",".join(t.lower().replace(" ", "_") for t in toolkits if t)
    async with httpx.AsyncClient() as client:
        res = await client.get(
            f"{COMPOSIO_V2}/actions",
            params={"apps": apps, "limit": limit},
            headers=_composio_headers(),
            timeout=20,
        )
        res.raise_for_status()
        data = res.json()

    schemas = []
    for action in data.get("items", []):
        params = action.get("parameters") or {"type": "object", "properties": {}}
        schemas.append({
            "type": "function",
            "function": {
                "name": action.get("name") or action.get("appKey"),
                "description": action.get("description") or "",
                "parameters": params,
            },
        })
    return schemas


@mcp.tool
async def sync_composio_connections(brand_id: str | None = None) -> dict:
    """Reconcile composio_connections with Composio's truth.
    Pulls /connected_accounts (filtered to brand if brand_id is given), upserts
    each ACTIVE row into the table, and marks any local row whose connection_id
    no longer exists in Composio as INACTIVE. Returns {created, updated, deactivated}."""
    user_id = _request_user_id.get()
    if not user_id:
        return {"error": "No user identity available"}
    if not _effective_composio_key():
        return {"error": "COMPOSIO_API_KEY not configured"}

    # Fetch Composio truth.
    params: dict = {"limit": 200}
    if brand_id:
        params["user_ids"] = brand_id
    data = await _composio_get("/connected_accounts", params)
    items = data.get("items") or []

    sb = _user_supabase()
    created = 0
    updated = 0

    composio_ids: set[str] = set()
    for it in items:
        conn_id = it.get("id") or it.get("connectionId")
        if not conn_id:
            continue
        composio_ids.add(conn_id)
        toolkit_slug = (
            (it.get("toolkit") or {}).get("slug")
            or it.get("toolkit_slug")
            or it.get("appName")
            or ""
        ).lower()
        if not toolkit_slug:
            continue
        status = it.get("status") or "ACTIVE"
        display_name = it.get("display_name") or it.get("displayName")
        entity_id = it.get("user_id") or it.get("userId") or it.get("entity_id")
        row_brand_id = entity_id if (entity_id and entity_id != "default" and _UUID_RE.match(entity_id)) else None

        row: dict = {
            "user_id": user_id,
            "toolkit_slug": toolkit_slug,
            "connection_id": conn_id,
            "status": status,
        }
        if display_name:
            row["display_name"] = display_name
        if row_brand_id:
            row["brand_id"] = row_brand_id

        try:
            res = sb.table("composio_connections").upsert(
                row, on_conflict="user_id,toolkit_slug,connection_id"
            ).execute()
            if res.data:
                # Heuristic: upsert returns the row regardless. Count both.
                updated += 1
        except Exception as e:
            print(f"[sync] upsert failed for {conn_id}: {e}")

    # Mark stale local rows INACTIVE.
    deactivated = 0
    try:
        local_rows = sb.table("composio_connections").select("id, connection_id, status").eq("user_id", user_id).execute().data or []
        stale = [r for r in local_rows if r.get("status") == "ACTIVE" and r.get("connection_id") not in composio_ids]
        for r in stale:
            sb.table("composio_connections").update({"status": "INACTIVE"}).eq("id", r["id"]).execute()
            deactivated += 1
    except Exception as e:
        print(f"[sync] stale-mark failed: {e}")

    return {
        "ok": True,
        "fetched_from_composio": len(composio_ids),
        "upserted": updated,
        "created_or_updated": created + updated,
        "deactivated": deactivated,
    }


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
