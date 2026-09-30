import json
import os
import tempfile
import threading

from datetime import datetime, timedelta, timezone
from pathlib import Path

import boto3

from mcp.server.fastmcp import FastMCP

mcp = FastMCP(
    "Cloud Operations MCP Server",
    host="127.0.0.1",
    port=8000,
)

# Choose "fixture" or "live" with CLOUD_OPS_DATA_MODE.
DATA_MODE = os.getenv("CLOUD_OPS_DATA_MODE", "fixture").lower()
AWS_REGION = os.getenv("AWS_REGION", "us-east-1")
ECS_CLUSTER = os.getenv("ECS_CLUSTER", "cloud-ops-demo")
LOG_GROUP = os.getenv(
    "CLOUDWATCH_LOG_GROUP",
    "/ecs/cloud-ops-demo/checkout-api",
)
ALB_NAME = os.getenv("ALB_NAME", "cloud-ops-demo")
TARGET_GROUP_NAME = os.getenv(
    "TARGET_GROUP_NAME",
    "cloud-ops-demo-checkout",
)
ALARM_NAME = os.getenv(
    "CLOUDWATCH_ALARM_NAME",
    "cloud-ops-demo-checkout-5xx",
)
CHECKOUT_SERVICE = os.getenv("ECS_SERVICE", "checkout-api")
APPROVALS_FILE = Path(
    os.getenv(
        "APPROVALS_FILE",
        str(Path(__file__).resolve().with_name("approvals.json")),
    )
)
ROLLBACK_TASK_DEFINITION_ARN = os.getenv(
    "ROLLBACK_TASK_DEFINITION_ARN",
    "arn:aws:ecs:us-east-1:991731688366:task-definition/cloud-ops-checkout:12",
)
ROLLBACK_LOCK = threading.Lock()


def metadata() -> dict:
    return {
        "source": DATA_MODE,
        "region": AWS_REGION,
    }


def validate_mode() -> None:
    if DATA_MODE not in {"fixture", "live"}:
        raise ValueError(
            "CLOUD_OPS_DATA_MODE must be fixture or live"
        )


def get_task_release(task_definition: dict):
    """Read APP_RELEASE=v1 or APP_RELEASE=v2 from a task definition."""
    for container in task_definition.get("containerDefinitions", []):
        for item in container.get("environment", []):
            if item.get("name") == "APP_RELEASE":
                return item.get("value")
    return None


@mcp.tool()
def health_check() -> dict:
    """Check whether the Cloud Operations MCP server is healthy."""
    validate_mode()
    return {
        "status": "healthy",
        "service": "cloud-operations-mcp",
        **metadata(),
    }


@mcp.tool()
def get_recent_logs(
    service: str,
    lookback_minutes: int = 15,
) -> dict:
    """Get recent application errors and warnings for a service."""
    validate_mode()

    if DATA_MODE == "fixture":
        return {
            **metadata(),
            "service": service,
            "lookback_minutes": lookback_minutes,
            "events": [
                {
                    "level": "ERROR",
                    "message": "Database connection timeout after 3000 ms",
                },
                {
                    "level": "ERROR",
                    "message": "HikariPool connection is not available",
                },
                {
                    "level": "WARN",
                    "message": (
                        "Database connection pool exhausted: "
                        "active=100 max=100"
                    ),
                },
            ],
        }

    logs = boto3.client("logs", region_name=AWS_REGION)
    start_time = int(
        (
            datetime.now(timezone.utc)
            - timedelta(minutes=lookback_minutes)
        ).timestamp()
        * 1000
    )

    response = logs.filter_log_events(
        logGroupName=LOG_GROUP,
        startTime=start_time,
        filterPattern='{ $.level = "ERROR" || $.level = "WARN" }',
        limit=100,
        startFromHead=False,
    )

    events = []
    for event in response.get("events", []):
        message = event.get("message", "")

        if '"level": "ERROR"' in message:
            level = "ERROR"
        elif '"level": "WARN"' in message:
            level = "WARN"
        else:
            level = "UNKNOWN"

        events.append(
            {
                "timestamp": event.get("timestamp"),
                "level": level,
                "message": message,
            }
        )

    return {
        **metadata(),
        "service": service,
        "lookback_minutes": lookback_minutes,
        "log_group": LOG_GROUP,
        "events": events,
    }


@mcp.tool()
def get_service_metrics(
    service: str,
    lookback_minutes: int = 15,
) -> dict:
    """Get recent ALB HTTP 5xx metrics for a service."""
    validate_mode()

    if DATA_MODE == "fixture":
        return {
            **metadata(),
            "service": service,
            "lookback_minutes": lookback_minutes,
            "http_5xx_rate_percent": 18.2,
            "cpu_utilization_percent": 34.1,
            "memory_utilization_percent": 48.3,
            "database_connections": {
                "active": 100,
                "maximum": 100,
            },
        }

    elbv2 = boto3.client("elbv2", region_name=AWS_REGION)
    cloudwatch = boto3.client("cloudwatch", region_name=AWS_REGION)

    load_balancers = elbv2.describe_load_balancers(
        Names=[ALB_NAME]
    ).get("LoadBalancers", [])

    target_groups = elbv2.describe_target_groups(
        Names=[TARGET_GROUP_NAME]
    ).get("TargetGroups", [])

    if not load_balancers or not target_groups:
        return {
            **metadata(),
            "service": service,
            "error": "ALB or target group was not found",
            "http_5xx_count": None,
            "datapoints": 0,
            "metric_data_available": False,
        }

    load_balancer = load_balancers[0]
    target_group = target_groups[0]

    end_time = datetime.now(timezone.utc)
    start_time = end_time - timedelta(minutes=lookback_minutes)

    load_balancer_dimension = (
        load_balancer["LoadBalancerArn"]
        .split(":")[-1]
        .replace("loadbalancer/", "")
    )
    target_group_dimension = (
        target_group["TargetGroupArn"]
        .split(":")[-1]
        .replace("targetgroup/", "")
    )

    response = cloudwatch.get_metric_statistics(
        Namespace="AWS/ApplicationELB",
        MetricName="HTTPCode_Target_5XX_Count",
        Dimensions=[
            {
                "Name": "LoadBalancer",
                "Value": load_balancer_dimension,
            },
            {
                "Name": "TargetGroup",
                "Value": target_group_dimension,
            },
        ],
        StartTime=start_time,
        EndTime=end_time,
        Period=60,
        Statistics=["Sum"],
    )

    datapoints = response.get("Datapoints", [])

    # Missing datapoints mean unknown/unpublished, not a zero error count.
    total_5xx = (
        sum(float(point.get("Sum", 0)) for point in datapoints)
        if datapoints
        else None
    )

    return {
        **metadata(),
        "service": service,
        "lookback_minutes": lookback_minutes,
        "http_5xx_count": total_5xx,
        "datapoints": len(datapoints),
        "metric_data_available": bool(datapoints),
        "load_balancer": load_balancer["LoadBalancerArn"],
        "target_group": target_group["TargetGroupArn"],
    }


@mcp.tool()
def get_alarm_status() -> dict:
    """Get the current CloudWatch 5xx alarm state and reason."""
    validate_mode()

    if DATA_MODE == "fixture":
        return {
            **metadata(),
            "alarm_name": ALARM_NAME,
            "state": "ALARM",
            "reason": "Fixture incident: checkout returned HTTP 500 errors.",
        }

    cloudwatch = boto3.client("cloudwatch", region_name=AWS_REGION)
    response = cloudwatch.describe_alarms(AlarmNames=[ALARM_NAME])
    alarms = response.get("MetricAlarms", [])

    if not alarms:
        return {
            **metadata(),
            "alarm_name": ALARM_NAME,
            "state": "NOT_FOUND",
            "reason": "CloudWatch alarm was not found.",
        }

    alarm = alarms[0]
    updated_at = alarm.get("StateUpdatedTimestamp")

    return {
        **metadata(),
        "alarm_name": alarm["AlarmName"],
        "state": alarm["StateValue"],
        "reason": alarm.get("StateReason", ""),
        "updated_at": updated_at.isoformat() if updated_at else None,
    }


@mcp.tool()
def get_ecs_service_status(
    service: str,
) -> dict:
    """Get the current ECS service status and task counts."""
    validate_mode()

    if DATA_MODE == "fixture":
        return {
            **metadata(),
            "cluster": "ops-demo-cluster",
            "service": service,
            "status": "ACTIVE",
            "desired_tasks": 3,
            "running_tasks": 3,
            "pending_tasks": 0,
            "load_balancer_health": "HEALTHY",
        }

    ecs = boto3.client("ecs", region_name=AWS_REGION)
    response = ecs.describe_services(
        cluster=ECS_CLUSTER,
        services=[service],
    )
    services = response.get("services", [])

    if not services:
        return {
            **metadata(),
            "cluster": ECS_CLUSTER,
            "service": service,
            "error": "ECS service was not found",
        }

    service_data = services[0]
    desired = service_data["desiredCount"]
    running = service_data["runningCount"]

    return {
        **metadata(),
        "cluster": ECS_CLUSTER,
        "service": service,
        "status": service_data["status"],
        "task_definition": service_data["taskDefinition"],
        "desired_tasks": desired,
        "running_tasks": running,
        "pending_tasks": service_data["pendingCount"],
        "load_balancer_health": (
            "HEALTHY" if running >= desired else "DEGRADED"
        ),
    }


@mcp.tool()
def get_recent_deployments(
    service: str,
) -> dict:
    """Report current and known-good demo rollback task definitions."""
    validate_mode()

    if DATA_MODE == "fixture":
        return {
            **metadata(),
            "service": service,
            "current_task_definition": "cloud-ops-checkout:43",
            "current_release": "v2",
            "deployments": [
                {
                    "task_definition": "cloud-ops-checkout:43",
                    "release": "v2",
                    "status": "PRIMARY",
                    "stability": "CURRENT",
                },
                {
                    "task_definition": "cloud-ops-checkout:42",
                    "release": "v1",
                    "status": "PREVIOUS_STABLE",
                    "stability": "KNOWN_GOOD_DEMO_BASELINE",
                },
            ],
        }

    ecs = boto3.client("ecs", region_name=AWS_REGION)
    response = ecs.describe_services(
        cluster=ECS_CLUSTER,
        services=[service],
    )
    services = response.get("services", [])

    if not services:
        return {
            **metadata(),
            "cluster": ECS_CLUSTER,
            "service": service,
            "error": "ECS service was not found",
            "deployments": [],
        }

    current_arn = services[0].get("taskDefinition")
    if not current_arn:
        return {
            **metadata(),
            "cluster": ECS_CLUSTER,
            "service": service,
            "error": "ECS service has no task definition",
            "deployments": [],
        }

    task_arns = ecs.list_task_definitions(
        familyPrefix="cloud-ops-checkout",
        status="ACTIVE",
        sort="DESC",
        maxResults=20,
    ).get("taskDefinitionArns", [])

    if current_arn not in task_arns:
        task_arns.insert(0, current_arn)

    task_data_by_arn = {}
    for arn in task_arns:
        task_data = ecs.describe_task_definition(
            taskDefinition=arn
        )["taskDefinition"]
        containers = task_data.get("containerDefinitions", [])

        task_data_by_arn[arn] = {
            "release": get_task_release(task_data),
            "image": (
                containers[0].get("image")
                if containers
                else None
            ),
        }

    current_release = task_data_by_arn.get(current_arn, {}).get("release")

    # In this demo v1 is the known-good baseline and v2 is the
    # intentional fault. Task-definition revision numbers do not
    # determine whether a release is v1 or v2.
    previous_stable_arn = None
    if current_release == "v2":
        previous_stable_arn = next(
            (
                arn
                for arn in task_arns
                if task_data_by_arn.get(arn, {}).get("release") == "v1"
            ),
            None,
        )

    deployments = []
    for arn in task_arns:
        task_data = task_data_by_arn.get(arn, {})
        release = task_data.get("release")

        if arn == current_arn:
            status = "PRIMARY"
            stability = "CURRENT"
        elif arn == previous_stable_arn:
            status = "PREVIOUS_STABLE"
            stability = "KNOWN_GOOD_DEMO_BASELINE"
        else:
            status = "NOT_CURRENT"
            stability = "UNKNOWN"

        deployments.append(
            {
                "task_definition": arn.rsplit("/", 1)[-1],
                "release": release,
                "status": status,
                "stability": stability,
                "image": task_data.get("image"),
            }
        )

    return {
        **metadata(),
        "cluster": ECS_CLUSTER,
        "service": service,
        "current_task_definition": current_arn,
        "current_release": current_release,
        "deployments": deployments,
    }


def _load_approval_data():
    if not APPROVALS_FILE.is_file():
        raise FileNotFoundError(f"Approval file not found: {APPROVALS_FILE}")
    with APPROVALS_FILE.open(encoding="utf-8") as file:
        return json.load(file)


def _find_approval(data: object, approval_id: str) -> dict | None:
    if isinstance(data, list):
        records = data
    elif isinstance(data, dict) and isinstance(data.get("approvals"), list):
        records = data["approvals"]
    elif isinstance(data, dict) and isinstance(data.get("requests"), list):
        records = data["requests"]
    elif isinstance(data, dict) and isinstance(data.get(approval_id), dict):
        return data[approval_id]
    else:
        raise ValueError(
            "Approval file must contain a list, an approvals list, or a requests list."
        )

    for record in records:
        if isinstance(record, dict) and record.get("approval_id") == approval_id:
            return record
    return None


def _save_approval_data(data: object) -> None:
    temp_name = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=APPROVALS_FILE.parent,
            prefix=f".{APPROVALS_FILE.name}.",
            suffix=".tmp",
            delete=False,
        ) as file:
            temp_name = file.name
            json.dump(data, file, indent=2)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        os.chmod(temp_name, 0o600)
        os.replace(temp_name, APPROVALS_FILE)
    finally:
        if temp_name and os.path.exists(temp_name):
            os.unlink(temp_name)


@mcp.tool()
def execute_approved_rollback(approval_id: str) -> dict:
    """Execute an approved rollback to the configured, known-good v1 task definition."""
    validate_mode()

    if DATA_MODE != "live":
        return {
            **metadata(),
            "status": "BLOCKED",
            "reason": "Rollback execution is disabled in fixture mode.",
            "action_not_executed": True,
        }

    with ROLLBACK_LOCK:
        try:
            approval_data = _load_approval_data()
            approval = _find_approval(approval_data, approval_id)
        except (OSError, json.JSONDecodeError, ValueError) as error:
            return {
                **metadata(),
                "status": "BLOCKED",
                "reason": str(error),
                "action_not_executed": True,
            }

        if approval is None:
            return {
                **metadata(),
                "status": "BLOCKED",
                "reason": "Approval ID was not found.",
                "action_not_executed": True,
            }

        if approval.get("approval_status") != "APPROVED":
            return {
                **metadata(),
                "status": "BLOCKED",
                "reason": "A human must approve this request first.",
                "approval_status": approval.get("approval_status"),
                "action_not_executed": True,
            }

        if approval.get("action") != "rollback_ecs_service":
            return {
                **metadata(),
                "status": "BLOCKED",
                "reason": "This approval is not for an ECS rollback.",
                "action_not_executed": True,
            }

        if approval.get("service") != CHECKOUT_SERVICE:
            return {
                **metadata(),
                "status": "BLOCKED",
                "reason": "The approved service is not the configured checkout service.",
                "action_not_executed": True,
            }

        if approval.get("action_not_executed") is not True:
            return {
                **metadata(),
                "status": "BLOCKED",
                "reason": "This approval has already been used or is being processed.",
                "action_not_executed": approval.get("action_not_executed"),
            }

        if approval.get("execution_status") in {
            "EXECUTING",
            "EXECUTION_UNKNOWN",
            "DEPLOYMENT_STARTED",
            "EXECUTED",
        }:
            return {
                **metadata(),
                "status": "BLOCKED",
                "reason": "This approval has an existing execution attempt; inspect ECS before retrying.",
                "execution_status": approval.get("execution_status"),
                "action_not_executed": approval.get("action_not_executed"),
            }

        target_arn = ROLLBACK_TASK_DEFINITION_ARN
        target_name = target_arn.rsplit("/", 1)[-1]
        approved_target = approval.get("target_task_definition")

        if approved_target not in {target_arn, target_name}:
            return {
                **metadata(),
                "status": "BLOCKED",
                "reason": "The approved task definition does not match the configured v1 target.",
                "approved_target": approved_target,
                "configured_target": target_name,
                "action_not_executed": True,
            }

        ecs = boto3.client("ecs", region_name=AWS_REGION)

        try:
            target = ecs.describe_task_definition(
                taskDefinition=target_arn
            )["taskDefinition"]

            if (
                target.get("status") != "ACTIVE"
                or get_task_release(target) != "v1"
            ):
                return {
                    **metadata(),
                    "status": "BLOCKED",
                    "reason": "The configured target is not an ACTIVE v1 task definition.",
                    "action_not_executed": True,
                }

            response = ecs.describe_services(
                cluster=ECS_CLUSTER,
                services=[CHECKOUT_SERVICE],
            )
            services = response.get("services", [])

            if not services or services[0].get("status") != "ACTIVE":
                return {
                    **metadata(),
                    "status": "BLOCKED",
                    "reason": "The configured ECS service was not found or is not ACTIVE.",
                    "action_not_executed": True,
                }

            current_arn = services[0].get("taskDefinition")
            current_task = ecs.describe_task_definition(
                taskDefinition=current_arn
            )["taskDefinition"]
            current_release = get_task_release(current_task)

            if current_release != "v2":
                return {
                    **metadata(),
                    "status": "BLOCKED",
                    "reason": "Rollback is allowed only while the live service is on demo release v2.",
                    "current_task_definition": current_arn,
                    "current_release": current_release,
                    "action_not_executed": True,
                }

            deployment_data = get_recent_deployments(CHECKOUT_SERVICE)
            previous_stable = {
                item.get("task_definition")
                for item in deployment_data.get("deployments", [])
                if item.get("status") == "PREVIOUS_STABLE"
                and item.get("stability") == "KNOWN_GOOD_DEMO_BASELINE"
                and item.get("release") == "v1"
            }

            if target_name not in previous_stable:
                return {
                    **metadata(),
                    "status": "BLOCKED",
                    "reason": "The target is not reported as the live service's previous-stable v1 revision.",
                    "configured_target": target_name,
                    "previous_stable_targets": sorted(previous_stable),
                    "action_not_executed": True,
                }

            approval["execution_status"] = "EXECUTING"
            approval["execution_started_at"] = datetime.now(
                timezone.utc
            ).isoformat()
            _save_approval_data(approval_data)

            try:
                update_response = ecs.update_service(
                    cluster=ECS_CLUSTER,
                    service=CHECKOUT_SERVICE,
                    taskDefinition=target_arn,
                )
            except Exception as error:
                # A timeout may happen after AWS accepted the request. Block retries
                # until ECS has been inspected to avoid starting a duplicate deployment.
                approval["execution_status"] = "EXECUTION_UNKNOWN"
                approval["execution_error"] = str(error)
                approval["action_not_executed"] = False

                try:
                    _save_approval_data(approval_data)
                except OSError:
                    pass

                return {
                    **metadata(),
                    "status": "EXECUTION_UNKNOWN",
                    "reason": str(error),
                    "check_ecs_before_retry": True,
                    "action_not_executed": False,
                }

            updated_service = update_response.get("service", {})
            primary = next(
                (
                    item
                    for item in updated_service.get("deployments", [])
                    if item.get("status") == "PRIMARY"
                ),
                {},
            )

            approval["approval_status"] = "EXECUTED"
            approval["action_not_executed"] = False
            approval["executed_at"] = datetime.now(timezone.utc).isoformat()
            approval["execution_status"] = "DEPLOYMENT_STARTED"
            approval["execution_result"] = {
                "service": CHECKOUT_SERVICE,
                "task_definition": target_arn,
                "deployment_id": primary.get("id"),
                "rollout_state": primary.get("rolloutState", "UNKNOWN"),
            }

            try:
                _save_approval_data(approval_data)
            except OSError as error:
                return {
                    **metadata(),
                    "status": "DEPLOYMENT_STARTED_AUDIT_WRITE_FAILED",
                    "reason": str(error),
                    "service": CHECKOUT_SERVICE,
                    "task_definition": target_arn,
                    "action_not_executed": False,
                    "verify_before_retry": True,
                }

            return {
                **metadata(),
                "status": "DEPLOYMENT_STARTED",
                "service": CHECKOUT_SERVICE,
                "task_definition": target_arn,
                "deployment_id": primary.get("id"),
                "rollout_state": primary.get("rolloutState", "UNKNOWN"),
                "verify_with": [
                    "get_ecs_service_status",
                    "GET /health",
                    "GET /checkout",
                ],
                "action_not_executed": False,
            }
        except Exception as error:
            return {
                **metadata(),
                "status": "BLOCKED_OR_FAILED",
                "reason": str(error),
                "action_not_executed": True,
            }


if __name__ == "__main__":
    mcp.run(transport="streamable-http")