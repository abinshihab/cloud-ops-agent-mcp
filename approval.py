import json
import os
from datetime import datetime, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile
from uuid import uuid4


APPROVALS_FILE = Path(__file__).with_name(
    "approvals.json"
)

ALLOWED_ACTIONS = {
    "rollback_ecs_service",
}

ALLOWED_RISK_LEVELS = {
    "low",
    "medium",
    "high",
}


def load_approvals() -> list[dict]:
    """Load all approval requests from the JSON store."""

    if not APPROVALS_FILE.exists():
        return []

    try:
        with APPROVALS_FILE.open(
            "r",
            encoding="utf-8",
        ) as file:
            approvals = json.load(file)

    except json.JSONDecodeError as error:
        raise RuntimeError(
            f"Invalid JSON in {APPROVALS_FILE}"
        ) from error

    if not isinstance(approvals, list):
        raise RuntimeError(
            "Approval store must contain a JSON list"
        )

    return approvals


def save_approvals(
    approvals: list[dict],
) -> None:
    """Safely save all approval requests."""

    APPROVALS_FILE.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    temporary_file_path = None

    try:
        with NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=APPROVALS_FILE.parent,
            prefix="approvals-",
            suffix=".tmp",
            delete=False,
        ) as temporary_file:
            json.dump(
                approvals,
                temporary_file,
                indent=2,
                ensure_ascii=False,
            )

            temporary_file.write("\n")
            temporary_file.flush()
            os.fsync(temporary_file.fileno())

            temporary_file_path = Path(
                temporary_file.name
            )

        os.replace(
            temporary_file_path,
            APPROVALS_FILE,
        )

    finally:
        if (
            temporary_file_path is not None
            and temporary_file_path.exists()
        ):
            temporary_file_path.unlink()


def request_human_approval(
    action: str,
    service: str,
    target_task_definition: str,
    reason: str,
    risk: str,
) -> dict:
    """Create and persist a pending approval request."""

    if action not in ALLOWED_ACTIONS:
        raise ValueError(
            f"Action is not allowed: {action}"
        )

    if risk not in ALLOWED_RISK_LEVELS:
        raise ValueError(
            "Risk must be low, medium, or high"
        )

    service = service.strip()
    target_task_definition = (
        target_task_definition.strip()
    )
    reason = reason.strip()

    if not service:
        raise ValueError(
            "Service name is required"
        )

    if not target_task_definition:
        raise ValueError(
            "Target task definition is required"
        )

    if not reason:
        raise ValueError(
            "Approval reason is required"
        )

    approval = {
        "approval_id": (
            f"approval-{uuid4().hex[:8]}"
        ),
        "approval_status": "PENDING",
        "action": action,
        "service": service,
        "target_task_definition": (
            target_task_definition
        ),
        "reason": reason,
        "risk": risk,
        "action_not_executed": True,
        "requested_at": datetime.now(
            timezone.utc
        ).isoformat(),
        "reviewed_at": None,
        "reviewed_by": None,
        "decision_reason": None,
    }

    approvals = load_approvals()
    approvals.append(approval)
    save_approvals(approvals)

    return approval