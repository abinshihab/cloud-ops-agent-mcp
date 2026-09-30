import asyncio
import json

import boto3
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


bedrock = boto3.client(
    "bedrock-runtime",
    region_name="us-east-1",
)

tool_config = {
    "tools": [
        {
            "toolSpec": {
                "name": "get_recent_logs",
                "description": (
                    "Get recent application errors and warnings "
                    "for a cloud service."
                ),
                "inputSchema": {
                    "json": {
                        "type": "object",
                        "properties": {
                            "service": {
                                "type": "string",
                                "description": "Name of the service",
                            },
                            "lookback_minutes": {
                                "type": "integer",
                                "description": (
                                    "How many minutes of logs to retrieve"
                                ),
                            },
                        },
                        "required": ["service"],
                    }
                },
            }
        }
    ]
}


async def call_mcp_tool(tool_use: dict):
    server_url = "http://127.0.0.1:8000/mcp"

    async with streamable_http_client(server_url) as (
        read_stream,
        write_stream,
        _,
    ):
        async with ClientSession(
            read_stream,
            write_stream,
        ) as session:
            await session.initialize()

            return await session.call_tool(
                tool_use["name"],
                tool_use["input"],
            )


response = bedrock.converse(
    modelId="amazon.nova-lite-v1:0",
    system=[
        {
            "text": (
                "You are a cloud operations investigation agent. "
                "Use the available tools to collect evidence before "
                "reaching a conclusion."
            )
        }
    ],
    messages=[
        {
            "role": "user",
            "content": [
                {
                    "text": (
                        "The checkout-api service has an HTTP 5xx rate "
                        "of 18.2%. Investigate the incident."
                    )
                }
            ],
        }
    ],
    toolConfig=tool_config,
    inferenceConfig={
        "maxTokens": 300,
        "temperature": 0,
    },
)

print("Stop reason:", response["stopReason"])

tool_request = None

for content in response["output"]["message"]["content"]:
    if "text" in content:
        print("Model text:", content["text"])

    if "toolUse" in content:
        tool_request = content["toolUse"]

        print("Tool requested:")
        print(json.dumps(tool_request, indent=2))

if tool_request is None:
    raise RuntimeError("Bedrock did not request a tool")

mcp_result = asyncio.run(
    call_mcp_tool(tool_request)
)

print("\nMCP tool result:")

for content in mcp_result.content:
    if content.type == "text":
        print(content.text)