"""Speak MCP over stdio to server.py: list tools and call a few live ones."""
import asyncio, sys
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

async def main():
    params = StdioServerParameters(command=sys.executable, args=["server.py"])
    async with stdio_client(params) as (r, w), ClientSession(r, w) as s:
        await s.initialize()
        print("tools:", [t.name for t in (await s.list_tools()).tools])
        for name, args in [("instrument_profile", {"ticker": "tcs.ns"}),
                           ("market_snapshot", {"ticker": "TCS.NS", "look_back_days": 3})]:
            res = await s.call_tool(name, args)
            print(f"--- {name}:", res.content[0].text[:200])
asyncio.run(main())
