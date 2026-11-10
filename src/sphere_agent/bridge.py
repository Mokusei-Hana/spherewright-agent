import asyncio
import json
from contextlib import asynccontextmanager

from jsonschema import validate
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

PLAYBOOK = "spherewright://agent/playbooks/opening-movement-v1"
READS = {
    "spherewright_get_status",
    "spherewright_get_session_state",
    "spherewright_get_player_state",
    "spherewright_get_progression_state",
    "spherewright_get_recipe_catalog",
    "spherewright_get_action_result",
    "spherewright_get_gameplay_journal",
}
WRITES = {"spherewright_prepare_handcraft", "spherewright_commit_handcraft"}


class Bridge:
    def __init__(self, session, timeout):
        self.session, self.timeout, self.schemas = session, timeout, {}

    async def discover(self):
        cursor = None
        while True:
            page = await asyncio.wait_for(self.session.list_tools(cursor=cursor), self.timeout)
            self.schemas.update({t.name: t.inputSchema for t in page.tools})
            cursor = page.nextCursor
            if not cursor:
                break
        missing = READS - self.schemas.keys()
        if missing:
            raise RuntimeError(f"Missing upstream read tools: {sorted(missing)}")

    async def call(self, name, arguments):
        if name not in READS | WRITES:
            raise ValueError("Tool not allowed by MVP")
        if name not in self.schemas:
            raise ValueError(f"Installed MCP does not advertise {name}")
        validate(arguments, self.schemas[name])
        result = await asyncio.wait_for(self.session.call_tool(name, arguments), self.timeout)
        data = result.structuredContent
        if data is None:
            # Older MCP hosts can expose the structured envelope as a text JSON block.
            for block in result.content:
                if getattr(block, "type", None) == "text":
                    try:
                        candidate = json.loads(block.text)
                        if isinstance(candidate, dict) and "success" in candidate:
                            data = candidate
                            break
                    except ValueError:
                        pass
        if result.isError or not isinstance(data, dict) or data.get("success") is not True:
            raise RuntimeError(f"{name}: upstream error or unsupported result envelope")
        payload = data.get("status") if name == "spherewright_get_status" else data.get("result")
        if not isinstance(payload, dict):
            raise RuntimeError("Missing structured upstream payload")
        return payload


@asynccontextmanager
async def connect(config):
    params = StdioServerParameters(command=config["command"], args=config["args"])
    async with stdio_client(params) as (reader, writer):
        async with ClientSession(reader, writer) as session:
            await asyncio.wait_for(session.initialize(), config["timeout_seconds"])
            bridge = Bridge(session, config["timeout_seconds"])
            await bridge.discover()
            yield bridge
