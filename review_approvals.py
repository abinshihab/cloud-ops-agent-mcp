import argparse
import json
from datetime import datetime, timezone

from approval import load_approvals, save_approvals


DECISION_STATUS = {
    "approve": "APPROVED",
    "reject": "REJECTED",
}


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
    """Approve or reject one pending request."""

    approvals = load_approvals()

    selected_approval = None

    for approval in approvals:
        if approval.get("approval_id") == approval_id:
            selected_approval = approval
            break

    if selected_approval is None:
        raise ValueError(
            f"Approval request not found: {approval_id}"
        )

    current_status = selected_approval.get(
        "approval_status"
    )

    if current_status != "PENDING":
        raise ValueError(
            f"Approval request is already {current_status}"
        )

    reviewed_by = reviewed_by.strip()
    decision_reason = decision_reason.strip()

    if not reviewed_by:
        raise ValueError(
            "Reviewer name is required"
        )

    if not decision_reason:
        raise ValueError(
            "Decision reason is required"
        )

    selected_approval["approval_status"] = (
        DECISION_STATUS[decision]
    )

    selected_approval["reviewed_at"] = (
        datetime.now(timezone.utc).isoformat()
    )

    selected_approval["reviewed_by"] = reviewed_by
    selected_approval["decision_reason"] = (
        decision_reason
    )

    # Approval does not execute the rollback.
    selected_approval["action_not_executed"] = True

    save_approvals(approvals)

    return selected_approval


def create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Review Cloud Operations approval requests."
        )
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


def main() -> None:
    parser = create_parser()
    args = parser.parse_args()

    if args.command == "list":
        list_pending_approvals()
        return

    try:
        result = review_approval(
            approval_id=args.approval_id,
            decision=args.command,
            reviewed_by=args.reviewed_by,
            decision_reason=args.reason,
        )

    except ValueError as error:
        parser.error(str(error))

    print(
        json.dumps(
            result,
            indent=2,
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()