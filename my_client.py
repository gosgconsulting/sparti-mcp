"""
Client for connecting to a FastMCP server running locally or in Docker via HTTP.
Usage:
    export HOST_URL="http://localhost"
    export PORT=8080
    uv run my_client.py
"""
import os
import asyncio
from fastmcp import Client

PORT = os.getenv("PORT", "8080")
HOST_URL = os.getenv("HOST_URL", "http://localhost")
client = Client(f"{HOST_URL}:{PORT}/mcp")


async def main():
    async with client:
        # Smoke-test: fetch one profile row from Supabase
        result = await client.call_tool("query_table", {"table": "profiles", "limit": 1})
        print("query_table result:", result)


if __name__ == "__main__":
    asyncio.run(main())
