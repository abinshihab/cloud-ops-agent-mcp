# Cloud Operations MCP Demo

A controlled AWS incident-response demo using a local Python agent, Amazon Bedrock, MCP tools, and an ECS service.

## What the demo shows

The agent investigates a deliberately faulty checkout release, gathers evidence from AWS, proposes a rollback, and waits for a human decision. After approval, an operator invokes the MCP rollback tool. ECS switches the service to the known-good release, and the operator verifies recovery.

The checkout endpoint is a simulation. It does not process real purchases or payments.

## Current implementation and alerting scope

- The Python agent and FastMCP server run on the operator's workstation.
- The model is Amazon Nova Lite v1, invoked through Amazon Bedrock Runtime. Model ID: amazon.nova-lite-v1:0.
- The local MCP server uses AWS credentials to query CloudWatch, Application Load Balancer, and ECS APIs.
- The current demo is started manually: Terraform deploys the v2 fault, the operator sends requests, and then starts the agent.
- Automatic alert routing from CloudWatch through EventBridge, Lambda, and SNS is a proposed integration. It is not deployed in this demo.
- In the recorded run, the CloudWatch alarm could remain OK when metric data was missing and missing data was configured as non-breaching. Missing metric datapoints mean “unknown/unavailable,” not zero errors. Do not say an automatic CloudWatch alert fired unless it actually entered ALARM during the run.

## Incident story

| Stage | Expected observation |
|---|---|
| Baseline v1 | /health and /checkout return HTTP 200 |
| Faulty v2 | /health returns HTTP 200; /checkout returns HTTP 500 |
| Investigation | Logs show CHECKOUT_CONFIG_ERROR and “unsupported pricing rule”; ECS and telemetry provide additional evidence |
| Approval | The agent creates a PENDING request for a specific known-good v1 task definition |
| Action | A human approves; the operator invokes execute_approved_rollback through MCP |
| Recovery | ECS runs v1; /health and /checkout return HTTP 200 |

Rollback restores the known-good release. It does not permanently fix the faulty v2 application configuration.

## Architecture

1. The operator starts the Python agent locally.
2. The agent calls Amazon Bedrock Runtime for model reasoning and tool selection.
3. The MCP client calls tools exposed by the local FastMCP server at http://127.0.0.1:8000/mcp.
4. Read-only tools query CloudWatch logs and alarms, ALB metrics, and ECS service/deployment data.
5. The agent proposes a bounded rollback and records a pending approval request.
6. A human reviews and approves that exact request.
7. The operator runs the MCP rollback helper. The server validates the approved request and configured v1 target before calling ECS UpdateService.
8. The operator waits for ECS stability and checks both endpoints.

Approval does not execute the rollback by itself. The operator must invoke the separate MCP execution tool after approval.

## Prerequisites

- AWS CLI configured for the intended AWS account and region us-east-1.
- Terraform, Docker, and Python 3.11 installed.
- Access to invoke Amazon Nova Lite v1 through Bedrock in us-east-1.
- AWS credentials with the required read permissions for CloudWatch Logs, CloudWatch metrics/alarms, ELBv2, and ECS. The rollback path additionally needs narrowly scoped ecs:UpdateService permission for the demo service.
- The current public IPv4 address in cloud-ops-terraform/02-runtime/terraform.tfvars, as allowed_cidr with /32.

Do not commit AWS credentials, .env files, terraform.tfvars, Terraform state/plan files, or approvals.json. The repository .gitignore excludes local state and configuration files; keep it in place.

## 1. Prepare local Python dependencies

Run from the repository root:

~~~bash
./.venv/bin/python -m pip install --force-reinstall "mcp==1.30.0" boto3
./.venv/bin/python -c "from importlib.metadata import version; from mcp.server.fastmcp import FastMCP; print('MCP', version('mcp'), '| FastMCP import OK')"
~~~

Use the project virtual environment for all Python commands. MCP 2.x has incompatible API changes for this demo's FastMCP v1 code.

If the local Terraform variables file does not exist, copy the example and edit allowed_cidr:

~~~bash
cp cloud-ops-terraform/02-runtime/terraform.tfvars.example cloud-ops-terraform/02-runtime/terraform.tfvars
~~~

## 2. Create AWS resources and publish the demo image

Open Terminal 1 at the repository root, verify the AWS identity, then enter the Terraform directory:

~~~bash
aws sts get-caller-identity
cd cloud-ops-terraform
~~~

Create the ECR repository and push the checkout image:

~~~bash
terraform -chdir=01-registry init
terraform -chdir=01-registry validate
terraform -chdir=01-registry plan -out=registry.tfplan
terraform -chdir=01-registry apply registry.tfplan
bash push-image.sh
~~~

Wait for the image push to finish.

Create the runtime with healthy v1:

~~~bash
terraform -chdir=02-runtime init
terraform -chdir=02-runtime validate
terraform -chdir=02-runtime plan -var="demo_release=v1" -out=runtime.tfplan
terraform -chdir=02-runtime apply runtime.tfplan
~~~

Wait for “Apply complete.” ECS may take a few minutes to become healthy.

## 3. Start the MCP server

Open Terminal 2 at the repository root. Start this step after Terraform has created the runtime resources:

~~~bash
cd "/Users/administrator/Desktop/Abu Dhabi AWS Meetup/AWS Community Day 2026/cloud-ops-agent-mcp"

export ROLLBACK_TASK_DEFINITION_ARN="$(
  terraform -chdir=cloud-ops-terraform/02-runtime output -json task_definitions |
  ./.venv/bin/python -c 'import json,sys; print(json.load(sys.stdin)["v1"])'
)"

echo "Rollback target: $ROLLBACK_TASK_DEFINITION_ARN"

export CLOUD_OPS_DATA_MODE=live
./.venv/bin/python mcp_server.py
~~~

The echo should show the full Terraform ARN for v1. Leave Terminal 2 open while the server runs.

## 4. Verify the healthy baseline

In Terminal 1, still in cloud-ops-terraform:

~~~bash
DEMO_URL=$(terraform -chdir=02-runtime output -raw base_url)
curl --connect-timeout 5 --max-time 20 -i "$DEMO_URL/health"
curl --connect-timeout 5 --max-time 20 -i "$DEMO_URL/checkout"
~~~

Both should return HTTP 200 and report release v1.

## 5. Deploy the intentional v2 fault

In Terminal 1:

~~~bash
terraform -chdir=02-runtime plan -var="demo_release=v2" -out=fault.tfplan
terraform -chdir=02-runtime apply fault.tfplan
~~~

Wait for “Apply complete,” then test the endpoints:

~~~bash
DEMO_URL=$(terraform -chdir=02-runtime output -raw base_url)
curl --connect-timeout 5 --max-time 20 -i "$DEMO_URL/health"
curl --connect-timeout 5 --max-time 20 -i "$DEMO_URL/checkout"
~~~

Expected: /health returns HTTP 200 on v2 and /checkout returns HTTP 500. If the release or status is different, stop and correct the deployment before starting the agent.

Generate a short sequence of checkout failures to create recent log evidence:

~~~bash
for i in {1..12}; do
  curl --connect-timeout 5 --max-time 15 -sS -o /dev/null \
    -w 'checkout HTTP %{http_code}\n' "$DEMO_URL/checkout"
  sleep 5
done
~~~

## 6. Run the investigating agent

Open Terminal 3 at the repository root:

~~~bash
cd "/path/to/cloud-ops-agent-mcp"
./.venv/bin/python agent_with_approval.py
~~~

Review the investigation. The agent should inspect logs, alarm/metrics, ECS status, and deployments, then create a new PENDING approval request for the known-good v1 task definition.

Evidence interpretation:

- /checkout returning 500 and the matching application log are observed facts.
- A healthy /health endpoint or load balancer target does not prove checkout works.
- No metric datapoints means the metric is unavailable for that query window; it does not prove zero errors.
- The alarm can be OK because of its missing-data policy. Do not treat that alone as proof that no incident occurred.

## 7. Review and approve the request

In Terminal 3, list requests and identify the fresh request from this run:

~~~bash
./.venv/bin/python review_approvals.py list
~~~

Check the service, action, reason, and exact v1 target. Approve only the new ID created by this run:

~~~bash
./.venv/bin/python review_approvals.py approve "approval-REPLACE-WITH-CURRENT-ID" \
  --reviewed-by "Ahmed Bin Shehab" \
  --reason "Reviewed the incident evidence and approved rollback to the known-good v1 task definition."
~~~

Replace the example approval ID with the fresh ID. Do not reuse an old approval. Approval records the human decision; it does not execute the change.

## 8. Automatic rollback after human approval

Keep the MCP Server running in Terminal 2. When you approve the request in Step 7 using `review_approvals.py approve`, the script saves the human decision and automatically calls `execute_approved_rollback` through MCP.

Review the command output to confirm whether MCP reported success. Do not run `execute_approved_rollback.py` separately.

Proceed to the next step to wait for ECS to stabilize and verify recovery.
## 9. Verify recovery

In Terminal 1, still in cloud-ops-terraform, wait for ECS stability and inspect the active task definition:

~~~bash
aws ecs wait services-stable \
  --cluster cloud-ops-demo \
  --services checkout-api \
  --region us-east-1

aws ecs describe-services \
  --cluster cloud-ops-demo \
  --services checkout-api \
  --region us-east-1 \
  --query 'services[0].{TaskDefinition:taskDefinition,Desired:desiredCount,Running:runningCount}' \
  --output table
~~~

Then test the application:

~~~bash
DEMO_URL=$(terraform -chdir=02-runtime output -raw base_url)
curl --connect-timeout 5 --max-time 20 -i "$DEMO_URL/health"
curl --connect-timeout 5 --max-time 20 -i "$DEMO_URL/checkout"
~~~

Both should return HTTP 200 and report release v1.

Check that Terraform agrees with the recovered configuration:

~~~bash
terraform -chdir=02-runtime plan
~~~

Expect no changes. If Terraform proposes returning the service to v2, do not apply that plan; correct the release variable first.

## Demo closing line

> “The agent investigated the incident and requested approval for a rollback. A human reviewed and approved the request. The operator invoked the MCP rollback tool, and we verified that checkout recovered on v1. Approval and execution are separate steps.”

## Troubleshooting

- **ALB connection timeout:** Check that the current public IP matches allowed_cidr in 02-runtime/terraform.tfvars. If it changed, update the file and apply the reviewed Terraform change.
- **MCP import/API errors:** Confirm the server and client use ./.venv/bin/python and MCP version 1.30.0, not the Anaconda Python environment or MCP 2.x.
- **Alarm says OK or the metric has no datapoints:** Report exactly that. Missing datapoints are not zero and the automatic alert path is not part of this demo.
- **Rollback helper fails:** Confirm the MCP server is still running, the new request is APPROVED, and the approved target matches the configured known-good v1 task definition.

See Full_Demo_Runbook_AWS.md for the detailed rehearsal and cleanup checklist.

## 10. Clean up AWS resources after the demo

Stop the MCP server with Ctrl+C. In Terminal 1, from cloud-ops-terraform, destroy runtime resources first:

~~~bash
terraform -chdir=02-runtime destroy
~~~

Review the plan and type yes. After runtime cleanup succeeds, delete the ECR repository and image:

~~~bash
terraform -chdir=01-registry destroy
~~~

Review the plan and type yes. Keep the repository source code and local Terraform state until each destroy operation has completed successfully.

