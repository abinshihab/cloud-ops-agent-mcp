import asyncio
import json

import boto3
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


MODEL_ID = "amazon.nova-lite-v1:0"
MCP_SERVER_URL = "http://127.0.0.1:8000/mcp"

bedrock = boto3.client(
    "bedrock-runtime",
    region_name="us-east-1",
)

system_prompt = [
    {
        "text": (
            "You are a cloud operations investigation agent. "
            "Use the available tools to collect evidence before "
            "reaching a conclusion. Base your findings only on "
            "the returned evidence. This test has one tool. "
            "After receiving its result, summarize the findings "
            "without requesting the same tool again."
        )
    }
]

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
                                "minimum": 1,
                                "maximum": 60,
                            },
                        },
                        "required": ["service"],
                    }
                },
            }
        }
    ]
}

messages = [
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
]


async def call_mcp_tool(tool_use: dict):
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

            return await session.call_tool(
                tool_use["name"],
                tool_use["input"],
            )


# First Bedrock call: ask the model what tool it needs.
first_response = bedrock.converse(
    modelId=MODEL_ID,
    system=system_prompt,
    messages=messages,
    toolConfig=tool_config,
    inferenceConfig={
        "maxTokens": 500,
        "temperature": 0,
    },
)

assistant_message = first_response["output"]["message"]
messages.append(assistant_message)

print("First stop reason:", first_response["stopReason"])

tool_request = None

for content in assistant_message["content"]:
    if "text" in content:
        print("Model text:", content["text"])

    if "toolUse" in content:
        tool_request = content["toolUse"]

        print("\nTool requested:")
        print(json.dumps(tool_request, indent=2))

if tool_request is None:
    raise RuntimeError("Bedrock did not request a tool")


# Execute the requested tool through MCP.
mcp_result = asyncio.run(
    call_mcp_tool(tool_request)
)

if not mcp_result.content:
    raise RuntimeError("MCP returned no content")

mcp_result_json = json.loads(
    mcp_result.content[0].text
)

print("\nMCP result:")
print(json.dumps(mcp_result_json, indent=2))


# Return the MCP result to Bedrock using the same toolUseId.
messages.append(
    {
        "role": "user",
        "content": [
            {
                "toolResult": {
                    "toolUseId": tool_request["toolUseId"],
                    "content": [
                        {
                            "json": mcp_result_json,
                        }
                    ],
                    "status": "success",
                }
            }
        ],
    }
)


# Second Bedrock call: let the model analyze the evidence.
final_response = bedrock.converse(
    modelId=MODEL_ID,
    system=system_prompt,
    messages=messages,
    toolConfig=tool_config,
    inferenceConfig={
        "maxTokens": 700,
        "temperature": 0,
    },
)

print("\nFinal stop reason:", final_response["stopReason"])
print("\nFinal assessment:")

for content in final_response["output"]["message"]["content"]:
    if "text" in content:
        print(content["text"])
