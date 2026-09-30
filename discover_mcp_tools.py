import asyncio
import json

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


MCP_SERVER_URL = "http://127.0.0.1:8000/mcp"


async def discover_tools() -> dict:
    async with streamable_http_client(MCP_SERVER_URL) as (
        read_stream,
        write_stream,
        _,
    ):
        async with ClientSession(
            read_stream,
            write_stream,
        ) as session:
            await session.initialize()

            mcp_result = await session.list_tools()

            bedrock_tools = []

            for tool in mcp_result.tools:
                # health_check monitors the MCP server itself.
                # It is not evidence for incident investigation.
                if tool.name == "health_check":
                    continue

                bedrock_tools.append(
                    {
                        "toolSpec": {
                            "name": tool.name,
                            "description": (
                                tool.description
                                or f"Execute {tool.name}"
                            ),
                            "inputSchema": {
                                "json": tool.inputSchema,
                            },
                        }
                    }
                )

            return {
                "tools": bedrock_tools,
            }


tool_config = asyncio.run(discover_tools())

print("Tools converted from MCP to Bedrock format:")

for tool in tool_config["tools"]:
    print(f"- {tool['toolSpec']['name']}")

print("\nComplete Bedrock tool configuration:")
print(json.dumps(tool_config, indent=2))
