import argparse
import asyncio
import json
import os
from datetime import datetime, timezone
from typing import Any

from approval import load_approvals, save_approvals


DECISION_STATUS = {
    "approve": "APPROVED",
    "reject": "REJECTED",
}

MCP_SERVER_URL = os.getenv(
    "MCP_SERVER_URL",
    "http://127.0.0.1:8000/mcp",
)
ROLLBACK_TOOL_NAME = "execute_approved_rollback"


def list_pending_approvals() -> None:
    """Display all pending approval requests."""
    approvals = load_approvals()
    pending_approvals = [
        approval
        for approval in approvals
        if approval.get("approval_status") == "PENDING"
    ]

    if not pending_approvals:
        print("No pending approval requests.")
        return

    print(
        json.dumps(
            pending_approvals,
            indent=2,
            ensure_ascii=False,
        )
    )


def review_approval(
    approval_id: str,
    decision: str,
    reviewed_by: str,
    decision_reason: str,
) -> dict:
    """Approve or reject one pending request and save the human decision."""
    approvals = load_approvals()
    selected_approval = next(
        (
            approval
            for approval in approvals
            if approval.get("approval_id") == approval_id
        ),
        None,
    )

    if selected_approval is None:
        raise ValueError(f"Approval request not found: {approval_id}")

    current_status = selected_approval.get("approval_status")
    if current_status != "PENDING":
        raise ValueError(f"Approval request is already {current_status}")

    reviewed_by = reviewed_by.strip()
    decision_reason = decision_reason.strip()

    if not reviewed_by:
        raise ValueError("Reviewer name is required")

    if not decision_reason:
        raise ValueError("Decision reason is required")

    selected_approval["approval_status"] = DECISION_STATUS[decision]
    selected_approval["reviewed_at"] = datetime.now(
        timezone.utc
    ).isoformat()
    selected_approval["reviewed_by"] = reviewed_by
    selected_approval["decision_reason"] = decision_reason

    # The approval is saved before MCP is called.
    # The MCP tool must verify the APPROVED record before changing ECS.
    selected_approval["action_not_executed"] = True
    save_approvals(approvals)

    return selected_approval


def mark_rollback_executed(approval_id: str) -> dict:
    """Record that MCP returned a successful rollback result."""
    approvals = load_approvals()
    selected_approval = next(
        (
            approval
            for approval in approvals
            if approval.get("approval_id") == approval_id
        ),
        None,
    )

    if selected_approval is None:
        raise ValueError(f"Approval request not found: {approval_id}")

    if selected_approval.get("approval_status") != "APPROVED":
        raise ValueError("The approval is not in APPROVED status")

    selected_approval["action_not_executed"] = False
    selected_approval["action_executed_at"] = datetime.now(
        timezone.utc
    ).isoformat()

    save_approvals(approvals)
    return selected_approval


def mcp_result_payload(result: Any) -> dict:
    """Convert an MCP tool result into printable JSON data."""
    structured = getattr(
        result,
        "structuredContent",
        getattr(result, "structured_content", None),
    )

    text_blocks = []
    parsed_blocks = []

    for content in getattr(result, "content", []):
        if getattr(content, "type", None) != "text":
            continue

        text = content.text
        text_blocks.append(text)

        try:
            parsed_blocks.append(json.loads(text))
        except json.JSONDecodeError:
            parsed_blocks.append(text)

    return {
        "is_error": bool(
            getattr(result, "isError", getattr(result, "is_error", False))
        ),
        "structured_content": structured,
        "content": parsed_blocks or text_blocks,
    }


def mcp_payload_indicates_failure(payload: dict) -> bool:
    """Detect MCP errors or explicit reports that the action did not run."""
    if payload.get("is_error"):
        return True

    candidates = [payload.get("structured_content")]
    content = payload.get("content", [])
    candidates.extend(content if isinstance(content, list) else [content])

    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue

        status = str(candidate.get("status", "")).lower()

        if (
            candidate.get("error")
            or candidate.get("action_not_executed") is True
            or status in {"error", "failed", "blocked"}
        ):
            return True

    return False


async def execute_approved_rollback_via_mcp(
    approval_id: str,
) -> dict:
    """Call the MCP rollback tool after the human approval is saved."""
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    async with streamable_http_client(MCP_SERVER_URL) as transport:
        if len(transport) == 2:
            read_stream, write_stream = transport
        elif len(transport) == 3:
            read_stream, write_stream, _ = transport
        else:
            raise RuntimeError(
                "Unexpected MCP transport result: expected 2 or 3 values, "
                f"received {len(transport)}"
            )

        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            result = await session.call_tool(
                ROLLBACK_TOOL_NAME,
                {"approval_id": approval_id},
            )

    payload = mcp_result_payload(result)

    if mcp_payload_indicates_failure(payload):
        raise RuntimeError(
            "MCP did not confirm rollback execution: "
            + json.dumps(payload, ensure_ascii=False, default=str)
        )

    return payload


def create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Review Cloud Operations approval requests."
    )

    subparsers = parser.add_subparsers(
        dest="command",
        required=True,
    )

    subparsers.add_parser(
        "list",
        help="List pending approval requests.",
    )

    for command in DECISION_STATUS:
        review_parser = subparsers.add_parser(
            command,
            help=f"{command.title()} a pending request.",
        )

        review_parser.add_argument(
            "approval_id",
            help="Approval request ID.",
        )

        review_parser.add_argument(
            "--reviewed-by",
            required=True,
            help="Name of the human reviewer.",
        )

        review_parser.add_argument(
            "--reason",
            required=True,
            help="Reason for the human decision.",
        )

    return parser


def main() -> int:
    parser = create_parser()
    args = parser.parse_args()

    if args.command == "list":
        list_pending_approvals()
        return 0

    try:
        approval = review_approval(
            approval_id=args.approval_id,
            decision=args.command,
            reviewed_by=args.reviewed_by,
            decision_reason=args.reason,
        )
    except ValueError as error:
        parser.error(str(error))

    print("Human decision saved:")
    print(
        json.dumps(
            approval,
            indent=2,
            ensure_ascii=False,
        )
    )

    # An explicit human approval triggers the MCP rollback tool.
    if (
        args.command == "approve"
        and approval.get("action") == "rollback_ecs_service"
    ):
        print("\nCalling MCP execute_approved_rollback...")

        try:
            execution_result = asyncio.run(
                execute_approved_rollback_via_mcp(args.approval_id)
            )
        except Exception as error:
            print(
                "MCP execution did not complete. The approval remains "
                "APPROVED; do not assume the rollback happened."
            )
            print(f"Details: {error}")
            return 1

        print("MCP tool result:")
        print(
            json.dumps(
                execution_result,
                indent=2,
                ensure_ascii=False,
                default=str,
            )
        )

        try:
            updated_approval = mark_rollback_executed(args.approval_id)
        except (OSError, ValueError) as error:
            print(
                "The MCP tool returned without an error, but the approval "
                "record could not be updated. Verify the ECS task definition "
                "before retrying."
            )
            print(f"Details: {error}")
            return 1

        print("\nApproval record updated:")
        print(
            json.dumps(
                updated_approval,
                indent=2,
                ensure_ascii=False,
                default=str,
            )
        )

    elif args.command == "reject":
        print("\nRequest rejected. No MCP action was executed.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())