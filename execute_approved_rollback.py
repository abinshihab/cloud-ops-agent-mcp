"""Invoke the MCP rollback tool for an already-approved request."""

import argparse
import asyncio
import os
import sys

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


async def execute_approved_rollback(approval_id: str, server_url: str) -> int:
    async with streamable_http_client(server_url) as (
        read_stream,
        write_stream,
        _,
    ):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            result = await session.call_tool(
                "execute_approved_rollback",
                {"approval_id": approval_id},
            )

            for item in result.content:
                if item.type == "text":
                    print(item.text)

            if result.isError:
                return 1

            return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Call the MCP rollback tool for an approval that a human "
            "has already approved."
        )
    )
    parser.add_argument(
        "approval_id",
        help="The fresh approval ID with APPROVED status.",
    )
    parser.add_argument(
        "--server-url",
        default=os.getenv(
            "MCP_SERVER_URL",
            "http://127.0.0.1:8000/mcp",
        ),
        help="MCP Server URL (default: %(default)s).",
    )
    args = parser.parse_args()

    try:
        return asyncio.run(
            execute_approved_rollback(
                approval_id=args.approval_id,
                server_url=args.server_url,
            )
        )
    except Exception as exc:
        print(f"Rollback tool call failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
