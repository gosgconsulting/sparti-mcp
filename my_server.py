import os
import inspect
from dotenv import load_dotenv
from supabase import create_client, Client
from fastmcp import FastMCP
import httpx
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

load_dotenv()

mcp = FastMCP("sparti-mcp")

supabase: Client = create_client(
    os.environ["SUPABASE_URL"],
    os.environ["SUPABASE_KEY"],
)

COMPOSIO_API_KEY = os.getenv("COMPOSIO_API_KEY", "")
COMPOSIO_V2 = "https://backend.composio.dev/api/v2"
COMPOSIO_V3 = "https://backend.composio.dev/api/v3"


def _composio_headers() -> dict:
    return {"x-api-key": COMPOSIO_API_KEY, "Content-Type": "application/json"}


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
    if not COMPOSIO_API_KEY:
        return [{"error": "COMPOSIO_API_KEY not configured in .env"}]
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
    entity_id identifies the connected user account (default: 'default')."""
    if not COMPOSIO_API_KEY:
        return {"error": "COMPOSIO_API_KEY not configured in .env"}
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
