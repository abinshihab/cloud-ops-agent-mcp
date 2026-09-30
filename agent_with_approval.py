import asyncio
import json
import re

import boto3
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from approval import load_approvals, request_human_approval


MODEL_ID = "amazon.nova-lite-v1:0"
MCP_SERVER_URL = "http://127.0.0.1:8000/mcp"
MAX_AGENT_TURNS = 10

REQUIRED_INVESTIGATION_TOOLS = {
    "get_recent_logs",
    "get_service_metrics",
    "get_alarm_status",
    "get_ecs_service_status",
    "get_recent_deployments",
}

bedrock = boto3.client("bedrock-runtime", region_name="us-east-1")

system_prompt = [
    {
        "text": (
            "You are an AWS cloud operations investigation agent. "
            "Investigate incidents using the available read-only tools. "
            "Treat the alarm as a starting signal, not proof of root cause. "
            "Before producing the final assessment, successfully check recent "
            "logs, service metrics, the CloudWatch alarm, ECS service status, "
            "and recent deployments. Base every conclusion on returned evidence. "
            "Use each investigation tool no more than once per incident. "
            "Never repeat a successful tool call with the same inputs. "
            "Clearly distinguish confirmed evidence from inference. "
            "Missing metric datapoints mean the metric is unavailable; they are "
            "not a zero value, a failure mechanism, or proof of an incident. "
            "Empty logs mean no matching events were returned in the requested "
            "window. If the alarm is OK, logs contain no errors, no 5xx "
            "datapoints are available, and ECS is ACTIVE with a healthy load "
            "balancer, report that no active incident is confirmed and do not "
            "recommend remediation or request approval. A deployment marked "
            "NOT_CURRENT only means it is not current; it does not prove that "
            "it was unstable or stable. Only report stability that a tool "
            "explicitly returns. Do not recommend scaling unless CPU, memory, "
            "or capacity metrics provide evidence that scaling is required. "
            "Temporal correlation with a deployment is not proof of causation. "
            "Do not say that a deployment caused, introduced, or led to an "
            "incident based only on timing. In that case, describe the "
            "deployment only as a suspected trigger or a correlated change. "
            "State the confirmed failure mechanism separately from the "
            "suspected trigger. In the Probable cause section, always use "
            "three separate labels: Confirmed failure mechanism, Suspected "
            "trigger, and Causation status. A full database connection pool "
            "confirms connection exhaustion, but it does not prove high "
            "traffic, insufficient capacity, or a deployment bug. Never say "
            "that a deployment likely caused the incident unless a tool "
            "returns direct causal evidence. The Causation status must only "
            "describe whether direct causal evidence exists. It must never "
            "contain an approval state. The allowed causation values are "
            "Confirmed or Unconfirmed. When evidence shows only temporal "
            "correlation, write: Causation status: Unconfirmed - temporal "
            "correlation exists, but direct causation has not been proven. "
            "Approval status is a separate section. A human approval is "
            "PENDING only if request_human_approval successfully created a "
            "pending record. A guardrail block is not a human rejection and "
            "creates no pending approval. Never say that you will proceed "
            "with remediation after approval because this agent cannot execute "
            "remediation. State that the request is pending and no action was "
            "executed only when a pending record was actually created. Do not "
            "mention duplicate tool calls in the final assessment unless they "
            "prevented the investigation from completing. If you recommend a "
            "rollback, restart, scaling change, or configuration change, you "
            "must call request_human_approval before producing the final answer. "
            "A pending approval does not mean the action was approved or "
            "executed. When requesting approval for an ECS rollback, specify "
            "the exact service and target task definition returned by the "
            "deployment evidence. Never claim that remediation was executed. "
            "Your final answer must contain these headings: Impact, Evidence, "
            "Probable cause, Recommended next steps, Approval status."
        )
    }
]


def create_initial_messages() -> list[dict]:
    """Create a new conversation using live evidence, not sample values."""
    return [
        {
            "role": "user",
            "content": [
                {
                    "text": (
                        "Investigate the current checkout-api incident in "
                        "us-east-1. Use the live tools to retrieve the alarm "
                        "state, recent logs, metrics, ECS service status, and "
                        "deployments. Do not assume an alarm state or metric "
                        "value."
                    )
                }
            ],
        }
    ]


def convert_tools_for_bedrock(mcp_tools) -> dict:
    """Convert MCP tools and add the local approval boundary."""
    bedrock_tools = []

    for tool in mcp_tools:
        if tool.name == "health_check":
            continue

        bedrock_tools.append(
            {
                "toolSpec": {
                    "name": tool.name,
                    "description": tool.description or f"Execute {tool.name}",
                    "inputSchema": {
                        "json": getattr(tool, "input_schema", None)
                        or getattr(tool, "inputSchema", None)
                    },
                }
            }
        )

    bedrock_tools.append(
        {
            "toolSpec": {
                "name": "request_human_approval",
                "description": (
                    "Create a pending human approval request for an exact ECS "
                    "rollback. This tool does not approve or execute the "
                    "rollback. The reason must distinguish confirmed evidence "
                    "from temporal correlation."
                ),
                "inputSchema": {
                    "json": {
                        "type": "object",
                        "properties": {
                            "action": {
                                "type": "string",
                                "enum": ["rollback_ecs_service"],
                            },
                            "service": {
                                "type": "string",
                                "description": "Exact ECS service name",
                            },
                            "target_task_definition": {
                                "type": "string",
                                "description": (
                                    "Exact previous stable task definition "
                                    "returned by the deployment tool"
                                ),
                            },
                            "reason": {
                                "type": "string",
                                "description": (
                                    "Evidence-based reason for requesting the "
                                    "rollback. Do not claim causation when "
                                    "only correlation is known."
                                ),
                            },
                            "risk": {
                                "type": "string",
                                "enum": ["low", "medium", "high"],
                            },
                        },
                        "required": [
                            "action",
                            "service",
                            "target_task_definition",
                            "reason",
                            "risk",
                        ],
                    }
                },
            }
        }
    )

    return {"tools": bedrock_tools}


def parse_mcp_result(result) -> dict:
    """Convert an MCP result into a JSON dictionary."""
    structured_content = getattr(
        result,
        "structured_content",
        getattr(result, "structuredContent", None),
    )

    if isinstance(structured_content, dict):
        return structured_content

    for content in result.content:
        if content.type == "text":
            try:
                return json.loads(content.text)
            except json.JSONDecodeError:
                return {"text": content.text}

    return {"error": "MCP tool returned no readable content"}


def create_call_key(tool_name: str, tool_input: dict) -> str:
    """Create a stable identifier for successful-call detection."""
    return json.dumps(
        {"tool": tool_name, "input": tool_input},
        sort_keys=True,
    )


def extract_allowed_rollback_targets(
    service: str,
    deployments_result: dict,
) -> set[tuple[str, str]]:
    """Extract only task definitions marked as previous stable."""
    allowed_targets = set()
    result_service = deployments_result.get("service", service)

    if result_service != service:
        return allowed_targets

    for deployment in deployments_result.get("deployments", []):
        if deployment.get("status") != "PREVIOUS_STABLE":
            continue

        task_definition = deployment.get("task_definition")
        if task_definition:
            allowed_targets.add((service, task_definition))

    return allowed_targets


def has_active_incident_evidence(results: dict) -> bool:
    """Return true only when live tools provide a current failure signal."""
    alarm = results.get("get_alarm_status", {})
    alarm_state = alarm.get("state", alarm.get("StateValue", ""))

    if str(alarm_state).upper() == "ALARM":
        return True

    metrics = results.get("get_service_metrics", {})
    count = metrics.get("http_5xx_count")

    try:
        if count is not None and float(count) > 0:
            return True
    except (TypeError, ValueError):
        pass

    logs = results.get("get_recent_logs", {})

    for event in logs.get("events", []):
        level = str(event.get("level", "")).upper()
        status_code = event.get("status_code")

        try:
            has_server_error = (
                status_code is not None and int(status_code) >= 500
            )
        except (TypeError, ValueError):
            has_server_error = False

        if level == "ERROR" or has_server_error:
            return True

    return False


def get_current_release(deployments_result: dict) -> str | None:
    """Read the current release from the live deployment response."""
    current_release = deployments_result.get("current_release")

    if current_release:
        return str(current_release)

    for deployment in deployments_result.get("deployments", []):
        if (
            deployment.get("status") == "PRIMARY"
            and deployment.get("release")
        ):
            return str(deployment["release"])

    return None


def get_v1_rollback_target(deployments_result: dict) -> str | None:
    """Return v1 only when ECS marks it PREVIOUS_STABLE."""
    matches = [
        deployment.get("task_definition")
        for deployment in deployments_result.get("deployments", [])
        if deployment.get("status") == "PREVIOUS_STABLE"
        and deployment.get("release") == "v1"
        and deployment.get("task_definition")
    ]

    return matches[0] if len(matches) == 1 else None


def existing_pending_approval(service: str, target: str) -> dict | None:
    """Reuse an identical pending request instead of creating duplicates."""
    for approval in load_approvals():
        if (
            approval.get("approval_status") == "PENDING"
            and approval.get("action") == "rollback_ecs_service"
            and approval.get("service") == service
            and approval.get("target_task_definition") == target
        ):
            return approval

    return None


def verified_approval_section(
    approval: dict | None,
    attempted: bool,
) -> str:
    """Render approval status from the saved record, never model claims."""
    if approval and approval.get("approval_status") == "PENDING":
        return (
            "**Approval status**\n"
            f"- PENDING (approval ID: {approval.get('approval_id')}).\n"
            "- No rollback was executed. A human must review this request."
        )

    if approval and approval.get("approval_status") in {"APPROVED", "REJECTED"}:
        return (
            "**Approval status**\n"
            f"- {approval['approval_status']} (approval ID: "
            f"{approval.get('approval_id')}).\n"
            "- This agent did not execute the rollback."
        )

    if attempted:
        return (
            "**Approval status**\n"
            "- No pending approval record was created. No action was executed."
        )

    return (
        "**Approval status**\n"
        "- No approval request was created. No action was executed."
    )


def remove_model_approval_section(text: str) -> str:
    """Remove model-written approval claims before adding verified status."""
    match = re.search(
        r"(?im)^\s*(?:\#{1,6}\s*)?(?:\*\*)?Approval status(?:\*\*)?\s*$",
        text,
    )

    if not match:
        return text.rstrip()

    return text[: match.start()].rstrip()


def build_incomplete_investigation_message(
    missing_tools: set[str],
) -> dict:
    """Tell the model which required evidence is still missing."""
    return {
        "role": "user",
        "content": [
            {
                "text": (
                    "The investigation is incomplete. You must call each "
                    "missing read-only investigation tool successfully before "
                    "requesting approval or producing the final assessment. "
                    f"Missing tools: {', '.join(sorted(missing_tools))}."
                )
            }
        ],
    }


async def run_agent():
    messages = create_initial_messages()

    async with streamable_http_client(MCP_SERVER_URL) as transport:
        if len(transport) == 2:
            read_stream, write_stream = transport
        elif len(transport) == 3:
            read_stream, write_stream, _ = transport
        else:
            raise RuntimeError(
                "Unexpected streamable HTTP transport result: "
                f"expected 2 or 3 values, got {len(transport)}"
            )

        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()

            discovered_tools = await session.list_tools()
            discovered_tool_names = {
                tool.name for tool in discovered_tools.tools
            }

            unavailable_required_tools = (
                REQUIRED_INVESTIGATION_TOOLS - discovered_tool_names
            )

            if unavailable_required_tools:
                missing_names = ", ".join(
                    sorted(unavailable_required_tools)
                )
                raise RuntimeError(
                    f"MCP server is missing required tools: {missing_names}"
                )

            tool_config = convert_tools_for_bedrock(discovered_tools.tools)
            tool_names = [
                tool["toolSpec"]["name"]
                for tool in tool_config["tools"]
            ]

            print("Available agent tools:")
            for tool_name in tool_names:
                print(f"- {tool_name}")

            successful_call_keys = set()
            successful_investigation_tools = set()
            allowed_rollback_targets = set()
            investigation_results = {}
            approval_record = None
            approval_attempted = False

            for turn in range(1, MAX_AGENT_TURNS + 1):
                print(f"\n========== Agent turn {turn} ==========")

                response = bedrock.converse(
                    modelId=MODEL_ID,
                    system=system_prompt,
                    messages=messages,
                    toolConfig=tool_config,
                    inferenceConfig={
                        "maxTokens": 1200,
                        "temperature": 0,
                    },
                )

                print(
                    "Bedrock usage:",
                    json.dumps(response.get("usage", {}), indent=2),
                )

                stop_reason = response["stopReason"]
                print("Stop reason:", stop_reason)

                assistant_message = response["output"]["message"]
                messages.append(assistant_message)

                tool_requests = []

                for content in assistant_message["content"]:
                    if "text" in content and stop_reason == "tool_use":
                        print("\nModel:")
                        print(content["text"])

                    if "toolUse" in content:
                        tool_requests.append(content["toolUse"])

                if not tool_requests:
                    missing_tools = (
                        REQUIRED_INVESTIGATION_TOOLS
                        - successful_investigation_tools
                    )

                    if missing_tools:
                        print(
                            "Guardrail: incomplete final assessment blocked"
                        )
                        print(
                            "Missing investigation tools:",
                            sorted(missing_tools),
                        )
                        messages.append(
                            build_incomplete_investigation_message(
                                missing_tools
                            )
                        )
                        continue

                    if stop_reason != "end_turn":
                        raise RuntimeError(
                            "Model stopped without completing the turn: "
                            f"{stop_reason}"
                        )

                    deployments = investigation_results.get(
                        "get_recent_deployments",
                        {},
                    )
                    current_release = get_current_release(deployments)
                    target = get_v1_rollback_target(deployments)

                    # Create the approval request only when live evidence
                    # indicates an incident and ECS confirms the v1 baseline.
                    if (
                        approval_record is None
                        and has_active_incident_evidence(
                            investigation_results
                        )
                        and current_release == "v2"
                        and target
                        and ("checkout-api", target)
                        in allowed_rollback_targets
                    ):
                        approval_attempted = True
                        approval_record = existing_pending_approval(
                            "checkout-api",
                            target,
                        )

                        if approval_record is None:
                            log_events = investigation_results.get(
                                "get_recent_logs",
                                {},
                            ).get("events", [])

                            error_count = sum(
                                1
                                for event in log_events
                                if str(event.get("level", "")).upper()
                                == "ERROR"
                            )

                            reason = (
                                f"Live investigation found {error_count} "
                                "recent ERROR log event(s) and the checkout "
                                "service is running release v2. ECS identifies "
                                f"{target} as the PREVIOUS_STABLE v1 baseline. "
                                "Requesting human review for rollback; the "
                                "evidence does not by itself prove the "
                                "deployment caused the errors. No rollback "
                                "is executed by this request."
                            )

                            print(
                                "\nTool selected: request_human_approval"
                            )

                            approval_record = request_human_approval(
                                action="rollback_ecs_service",
                                service="checkout-api",
                                target_task_definition=target,
                                reason=reason,
                                risk="medium",
                            )

                            print("Tool result:")
                            print(json.dumps(approval_record, indent=2))

                    print("\n========== Final assessment ==========")

                    final_text = "\n".join(
                        content["text"]
                        for content in assistant_message["content"]
                        if "text" in content
                    )
                    final_text = remove_model_approval_section(final_text)

                    if final_text:
                        print(final_text)
                        print()

                    print(
                        verified_approval_section(
                            approval_record,
                            attempted=approval_attempted,
                        )
                    )
                    return

                tool_results = []

                for tool_request in tool_requests:
                    tool_name = tool_request["name"]
                    tool_input = tool_request["input"]

                    print(f"\nTool selected: {tool_name}")
                    print("Tool input:", json.dumps(tool_input, indent=2))

                    call_key = create_call_key(tool_name, tool_input)

                    if call_key in successful_call_keys:
                        result_json = {
                            "error": (
                                "Duplicate successful tool call blocked by "
                                "the agent guardrail"
                            ),
                            "tool": tool_name,
                            "input": tool_input,
                        }
                        tool_status = "error"
                        print(
                            "Guardrail: duplicate successful call blocked"
                        )

                    elif tool_name == "request_human_approval":
                        missing_tools = (
                            REQUIRED_INVESTIGATION_TOOLS
                            - successful_investigation_tools
                        )

                        if missing_tools:
                            result_json = {
                                "error": (
                                    "Approval request blocked because the "
                                    "investigation is incomplete"
                                ),
                                "missing_tools": sorted(missing_tools),
                                "approval_status": "BLOCKED_BY_GUARDRAIL",
                                "human_decision_made": False,
                                "action_not_executed": True,
                            }
                            tool_status = "error"
                            print(
                                "Guardrail: premature approval request blocked"
                            )

                        else:
                            service = tool_input.get("service", "")
                            target_task_definition = tool_input.get(
                                "target_task_definition",
                                "",
                            )
                            requested_target = (
                                service,
                                target_task_definition,
                            )

                            if not has_active_incident_evidence(
                                investigation_results
                            ):
                                result_json = {
                                    "error": (
                                        "Approval request blocked: the live "
                                        "investigation did not find current "
                                        "incident evidence."
                                    ),
                                    "approval_status": "BLOCKED_BY_GUARDRAIL",
                                    "human_decision_made": False,
                                    "action_not_executed": True,
                                }
                                tool_status = "error"
                                print(
                                    "Guardrail: no active incident evidence; "
                                    "approval not submitted"
                                )

                            elif requested_target not in allowed_rollback_targets:
                                result_json = {
                                    "error": (
                                        "Approval request blocked: target was "
                                        "not explicitly returned as "
                                        "PREVIOUS_STABLE. This does not prove "
                                        "that the task definition is unstable."
                                    ),
                                    "requested_target": {
                                        "service": service,
                                        "task_definition": (
                                            target_task_definition
                                        ),
                                    },
                                    "allowed_targets": [
                                        {
                                            "service": allowed_service,
                                            "task_definition": (
                                                allowed_task_definition
                                            ),
                                        }
                                        for (
                                            allowed_service,
                                            allowed_task_definition,
                                        ) in sorted(allowed_rollback_targets)
                                    ],
                                    "approval_status": "BLOCKED_BY_GUARDRAIL",
                                    "human_decision_made": False,
                                    "action_not_executed": True,
                                }
                                tool_status = "error"
                                print(
                                    "Guardrail: invalid rollback target "
                                    "blocked"
                                )

                            else:
                                try:
                                    approval_attempted = True
                                    result_json = existing_pending_approval(
                                        service,
                                        target_task_definition,
                                    )

                                    if result_json is None:
                                        result_json = request_human_approval(
                                            action=tool_input["action"],
                                            service=service,
                                            target_task_definition=(
                                                target_task_definition
                                            ),
                                            reason=tool_input["reason"],
                                            risk=tool_input["risk"],
                                        )

                                    tool_status = "success"
                                    successful_call_keys.add(call_key)

                                    if (
                                        result_json.get("approval_status")
                                        == "PENDING"
                                    ):
                                        approval_record = result_json

                                except (KeyError, ValueError) as error:
                                    result_json = {
                                        "error": str(error),
                                        "action_not_executed": True,
                                    }
                                    tool_status = "error"

                    elif tool_name not in discovered_tool_names:
                        result_json = {
                            "error": "Unknown MCP tool requested",
                            "tool": tool_name,
                        }
                        tool_status = "error"
                        print("Guardrail: unknown tool blocked")

                    else:
                        try:
                            mcp_result = await session.call_tool(
                                tool_name,
                                tool_input,
                            )
                            result_json = parse_mcp_result(mcp_result)

                            tool_status = (
                                "error"
                                if (
                                    getattr(
                                        mcp_result,
                                        "is_error",
                                        getattr(mcp_result, "isError", False),
                                    )
                                    or result_json.get("error")
                                )
                                else "success"
                            )

                        except Exception as error:
                            result_json = {
                                "error": f"MCP tool call failed: {error}",
                                "tool": tool_name,
                            }
                            tool_status = "error"

                        if tool_status == "success":
                            successful_call_keys.add(call_key)
                            investigation_results[tool_name] = result_json

                            if tool_name in REQUIRED_INVESTIGATION_TOOLS:
                                successful_investigation_tools.add(tool_name)

                            if tool_name == "get_recent_deployments":
                                discovered_targets = (
                                    extract_allowed_rollback_targets(
                                        service=tool_input["service"],
                                        deployments_result=result_json,
                                    )
                                )
                                allowed_rollback_targets.update(
                                    discovered_targets
                                )
                                print(
                                    "Allowed rollback targets:",
                                    sorted(allowed_rollback_targets),
                                )

                    print("Tool result:")
                    print(json.dumps(result_json, indent=2))

                    tool_results.append(
                        {
                            "toolResult": {
                                "toolUseId": tool_request["toolUseId"],
                                "content": [{"json": result_json}],
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

            raise RuntimeError(
                f"Agent exceeded {MAX_AGENT_TURNS} turns"
            )


if __name__ == "__main__":
    asyncio.run(run_agent())