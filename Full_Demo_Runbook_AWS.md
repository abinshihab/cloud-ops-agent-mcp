# Full Demo Runbook: From AWS Setup to Cleanup

Use this runbook to prepare, present, and clean up the AWS demo. The live presentation demonstrates the fault, investigation, human approval, rollback, and recovery. Create the AWS resources before the presentation.

## 1. Check AWS access and project settings

In Terminal 1, go to the project directory:

```bash
cd "/Users/administrator/Desktop/Abu Dhabi AWS Meetup/AWS Community Day 2026/cloud-ops-agent-mcp"
aws sts get-caller-identity
```

Confirm that the AWS account is correct. Check that `cloud-ops-terraform/02-runtime/terraform.tfvars` exists and that `allowed_cidr` contains your current public IP address with `/32`.

## 2. Create the ECR repository and push the image

From the project directory:

```bash
cd cloud-ops-terraform

terraform -chdir=01-registry init
terraform -chdir=01-registry validate
terraform -chdir=01-registry plan -out=registry.tfplan
terraform -chdir=01-registry apply registry.tfplan

bash push-image.sh
```

Wait for the image push to complete.

## 3. Create the runtime with healthy v1

Remain in the `cloud-ops-terraform` directory:

```bash
terraform -chdir=02-runtime init
terraform -chdir=02-runtime validate
terraform -chdir=02-runtime plan \
  -var="demo_release=v1" \
  -out=runtime.tfplan
terraform -chdir=02-runtime apply runtime.tfplan
```

Wait for `Apply complete`. ECS may take a few minutes to become healthy.

## 4. Pin the MCP SDK and start the MCP server

In Terminal 2, from the `cloud-ops-agent-mcp` project directory:

```bash
cd "/Users/administrator/Desktop/Abu Dhabi AWS Meetup/AWS Community Day 2026/cloud-ops-agent-mcp"

./.venv/bin/python -m pip install --force-reinstall "mcp==1.30.0"

./.venv/bin/python -c "from importlib.metadata import version; from mcp.server.fastmcp import FastMCP; print('MCP', version('mcp'), '| FastMCP import OK')"

export CLOUD_OPS_DATA_MODE=live
./.venv/bin/python mcp_server.py
```

Confirm the output shows MCP version `1.30.0` and `FastMCP import OK`. Leave this terminal open while the agent runs. Use `./.venv/bin/python` for project Python commands so the shell does not select the Anaconda Python with MCP 2.x.

## 5. Verify the healthy baseline

In Terminal 1, still in `cloud-ops-terraform`:

```bash
DEMO_URL=$(terraform -chdir=02-runtime output -raw base_url)

curl -i "$DEMO_URL/health"
curl -i "$DEMO_URL/checkout"
```

Both requests should return HTTP 200 and report release `v1`.

## 6. Deploy the intentional fault, v2

In Terminal 1:

```bash
terraform -chdir=02-runtime plan \
  -var="demo_release=v2" \
  -out=fault.tfplan

terraform -chdir=02-runtime apply fault.tfplan
```

Wait for `Apply complete`.

## 7. Show the failure and generate requests

In Terminal 1:

```bash
DEMO_URL=$(terraform -chdir=02-runtime output -raw base_url)

curl -i "$DEMO_URL/health"
curl -i "$DEMO_URL/checkout"
```

Expected result: `/health` returns HTTP 200 with release `v2`, while `/checkout` returns HTTP 500.

Check the release before continuing:

```bash
curl --connect-timeout 5 --max-time 20 -i "$DEMO_URL/health"
curl --connect-timeout 5 --max-time 20 -i "$DEMO_URL/checkout"
```

If health reports `v1` or checkout returns HTTP 200, stop here and do not run the agent yet. Reapply the v2 deployment from Step 6:

```bash
terraform -chdir=02-runtime plan -var="demo_release=v2" -out=fault.tfplan
terraform -chdir=02-runtime apply fault.tfplan
```

Wait for `Apply complete`, then repeat the two curl checks. Proceed only when health reports `v2` and checkout returns HTTP 500.

To generate more error logs, run:

```bash
for i in {1..12}; do
  curl --connect-timeout 5 --max-time 15 -sS -o /dev/null -w 'checkout HTTP %{http_code}\n' "$DEMO_URL/checkout"
  sleep 5
done
```

## 8. Run the agent and request human approval

In Terminal 3, from the `cloud-ops-agent-mcp` project directory:

```bash
cd "/Users/administrator/Desktop/Abu Dhabi AWS Meetup/AWS Community Day 2026/cloud-ops-agent-mcp"
source .venv/bin/activate
./.venv/bin/python agent_with_approval.py
```

Show the agent’s investigation and its new approval request. It should have status `PENDING`, and the action should not have been executed.

## 9. Review and approve the request

In Terminal 3:

```bash
./.venv/bin/python review_approvals.py list
```

Review the new request’s service, target task definition, and reason. Approve the new approval ID created in the current run:

```bash
./.venv/bin/python review_approvals.py approve <NEW_APPROVAL_ID> \
  --reviewed-by "Ahmed Bin Shehab" \
  --reason "Reviewed the incident evidence and approved rollback to the known-good v1 task definition."
```

Do not approve an ID left over from an earlier run. Approval records the human decision; it does not run Terraform.

## 10. Perform the rollback after approval

In Terminal 1, still in `cloud-ops-terraform`:

```bash
terraform -chdir=02-runtime plan \
  -var="demo_release=v1" \
  -out=recovery.tfplan
```

Check that the target task definition in the Terraform plan matches the target in the approved request. Then apply:

```bash
terraform -chdir=02-runtime apply recovery.tfplan
```

Wait for `Apply complete`.

## 11. Verify recovery

In Terminal 1:

```bash
DEMO_URL=$(terraform -chdir=02-runtime output -raw base_url)

curl -i "$DEMO_URL/health"
curl -i "$DEMO_URL/checkout"
```

Both requests should return HTTP 200 and report release `v1`.

**Closing line:**

> “The agent investigated the incident and requested approval for a rollback. A human reviewed and approved the request. I then performed the rollback with Terraform and verified that checkout recovered. Approval and execution are separate steps.”

## 12. Delete AWS resources after the demo

Stop the MCP server with `Ctrl+C`. In Terminal 1, from the `cloud-ops-terraform` directory, destroy the runtime first:

```bash
terraform -chdir=02-runtime destroy
```

Review the plan and type `yes`. After it completes, delete the ECR repository and image:

```bash
terraform -chdir=01-registry destroy
```

Review the plan and type `yes`.

## Important note

Task definition revision numbers can change if you recreate the environment. Always compare the new approval request with the current Terraform plan before applying the rollback.

