import asyncio

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


async def print_tool_result(session, tool_name, arguments, heading):
    result = await session.call_tool(tool_name, arguments)

    print(f"\n{heading}:")
    for content in result.content:
        if content.type == "text":
            print(content.text)


async def main():
    server_url = "http://127.0.0.1:8000/mcp"

    async with streamable_http_client(server_url) as (
        read_stream,
        write_stream,
        _,
    ):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()

            tools = await session.list_tools()

            print("Discovered tools:")
            for tool in tools.tools:
                print(f"- {tool.name}: {tool.description}")

            await print_tool_result(
                session,
                "health_check",
                {},
                "Health check",
            )

            await print_tool_result(
                session,
                "get_alarm_status",
                {},
                "Alarm status",
            )

            await print_tool_result(
                session,
                "get_recent_logs",
                {
                    "service": "checkout-api",
                    "lookback_minutes": 15,
                },
                "Recent logs",
            )

            await print_tool_result(
                session,
                "get_service_metrics",
                {
                    "service": "checkout-api",
                    "lookback_minutes": 15,
                },
                "Service metrics",
            )

            await print_tool_result(
                session,
                "get_ecs_service_status",
                {
                    "service": "checkout-api",
                },
                "ECS service status",
            )

            await print_tool_result(
                session,
                "get_recent_deployments",
                {
                    "service": "checkout-api",
                },
                "Recent deployments",
            )


if __name__ == "__main__":
    asyncio.run(main())