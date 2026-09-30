import asyncio
import json

import boto3
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


MODEL_ID = "amazon.nova-lite-v1:0"
AWS_REGION = "us-east-1"
MCP_SERVER_URL = "http://127.0.0.1:8000/mcp"
SERVICE_NAME = "checkout-api"
LOOKBACK_MINUTES = 30
MAX_AGENT_TURNS = 8


bedrock = boto3.client(
    "bedrock-runtime",
    region_name=AWS_REGION,
)


system_prompt = [
    {
        "text": (
            "You are an AWS cloud operations investigation agent. "
            "Investigate the CURRENT state using the available "
            "read-only MCP tools. No alarm state, error rate, or active "
            "incident is assumed. Treat tool results as the source of "
            "truth. Check the alarm, recent logs, service metrics, ECS "
            "service status, and deployments before giving your assessment. "
            "Use each investigation tool no more than once. If a tool call "
            "is blocked as a duplicate, stop requesting tools and provide "
            "your final assessment using the evidence already returned. "
            "If metric_data_available is false or datapoints is zero, say "
            "that the metric is unavailable; do not report its value as "
            "zero. If the alarm is OK and there are no matching recent error "
            "or warning logs, say that the returned evidence does not show "
            "an active incident. Do not infer that a non-current deployment "
            "caused a problem, especially when its stability is UNKNOWN. "
            "Clearly distinguish evidence from inference. Do not claim any "
            "remediation was executed. Keep the answer concise and use these "
            "headings: Impact, Evidence, Probable cause, Recommended next steps."
        )
    }
]


messages = [
    {
        "role": "user",
        "content": [
            {
                "text": (
                    f"Investigate the current live state of "
                    f"{SERVICE_NAME} in {AWS_REGION}. "
                    f"Use a lookback period of {LOOKBACK_MINUTES} minutes "
                    "where a tool requests one. Do not assume that an "
                    "incident or alarm exists."
                )
            }
        ],
    }
]


def convert_tools_for_bedrock(mcp_tools) -> dict:
    """Convert MCP tool definitions into Bedrock tool specifications."""

    bedrock_tools = []

    for tool in mcp_tools:
        # This reports MCP server health, not application incident evidence.
        if tool.name == "health_check":
            continue

        bedrock_tools.append(
            {
                "toolSpec": {
                    "name": tool.name,
                    "description": (
                        tool.description or f"Execute {tool.name}"
                    ),
                    "inputSchema": {
                        "json": tool.inputSchema,
                    },
                }
            }
        )

    return {"tools": bedrock_tools}


def parse_mcp_result(result) -> dict:
    """Convert an MCP tool result into a JSON dictionary."""

    structured_content = getattr(result, "structuredContent", None)

    if isinstance(structured_content, dict):
        return structured_content

    for content in getattr(result, "content", []):
        if getattr(content, "type", None) == "text":
            try:
                parsed = json.loads(content.text)
                if isinstance(parsed, dict):
                    return parsed
                return {"result": parsed}
            except json.JSONDecodeError:
                return {"text": content.text}

    return {"error": "MCP tool returned no readable content"}


def print_final_assessment(response) -> None:
    """Print the model's final text response."""

    print("\n========== Final assessment ==========")

    message = response["output"]["message"]

    for content in message.get("content", []):
        if "text" in content:
            print(content["text"])


def request_final_without_tools():
    """
    Ask for a final answer without passing tools.
    This prevents another tool-use loop.
    """

    response = bedrock.converse(
        modelId=MODEL_ID,
        system=system_prompt,
        messages=messages,
        inferenceConfig={
            "maxTokens": 1000,
            "temperature": 0,
        },
    )

    print_final_assessment(response)


async def run_agent():
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

            discovered = await session.list_tools()
            tool_config = convert_tools_for_bedrock(discovered.tools)

            tool_names = [
                item["toolSpec"]["name"]
                for item in tool_config["tools"]
            ]

            print("Discovered investigation tools:")
            for name in tool_names:
                print(f"- {name}")

            # Enforce one call per tool name for this investigation.
            seen_tool_names = set()

            for turn in range(1, MAX_AGENT_TURNS + 1):
                print(f"\n========== Agent turn {turn} ==========")

                response = bedrock.converse(
                    modelId=MODEL_ID,
                    system=system_prompt,
                    messages=messages,
                    toolConfig=tool_config,
                    inferenceConfig={
                        "maxTokens": 1000,
                        "temperature": 0,
                    },
                )

                stop_reason = response["stopReason"]
                assistant_message = response["output"]["message"]
                messages.append(assistant_message)

                print("Stop reason:", stop_reason)

                tool_requests = [
                    content["toolUse"]
                    for content in assistant_message.get("content", [])
                    if "toolUse" in content
                ]

                # No tool request means the model has finished.
                if not tool_requests:
                    print_final_assessment(response)
                    return

                tool_results = []
                duplicate_blocked = False

                for request in tool_requests:
                    tool_name = request["name"]
                    tool_input = request.get("input", {})

                    print(f"\nTool selected: {tool_name}")
                    print("Tool input:")
                    print(json.dumps(tool_input, indent=2))

                    if tool_name in seen_tool_names:
                        duplicate_blocked = True
                        result_json = {
                            "error": (
                                "Duplicate tool call blocked. "
                                "Use the earlier result and finish "
                                "the assessment without calling tools again."
                            ),
                            "tool": tool_name,
                        }
                        tool_status = "error"
                        print("Guardrail: duplicate tool call blocked")

                    else:
                        seen_tool_names.add(tool_name)

                        try:
                            mcp_result = await session.call_tool(
                                tool_name,
                                tool_input,
                            )
                            result_json = parse_mcp_result(mcp_result)
                            tool_status = (
                                "error"
                                if getattr(mcp_result, "isError", False)
                                else "success"
                            )
                        except Exception as exc:
                            result_json = {
                                "error": str(exc),
                                "tool": tool_name,
                            }
                            tool_status = "error"

                    print("Tool result:")
                    print(json.dumps(result_json, indent=2))

                    # Bedrock expects one result for every toolUse request.
                    tool_results.append(
                        {
                            "toolResult": {
                                "toolUseId": request["toolUseId"],
                                "content": [
                                    {"json": result_json}
                                ],
                                "status": tool_status,
                            }
                        }
                    )

                messages.append(
                    {
                        "role": "user",
                        "content": tool_results,
                    }
                )

                # Do not let a duplicate-call response start another loop.
                if duplicate_blocked:
                    request_final_without_tools()
                    return

            # If the turn limit is reached, finish from collected evidence.
            print(
                f"\nReached the {MAX_AGENT_TURNS}-turn limit; "
                "requesting a final assessment without more tools."
            )
            request_final_without_tools()


if __name__ == "__main__":
    asyncio.run(run_agent())