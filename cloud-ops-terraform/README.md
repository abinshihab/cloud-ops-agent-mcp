# Cloud Ops demo — Terraform

Prepared for Ahmed's existing checkout-demo:local Docker image. Region: us-east-1.
Both providers restrict deployment to AWS account 991731688366. No credentials
are embedded. Terraform uses the AWS CLI credential chain / AWS_PROFILE.

## What is included

- 01-registry: dedicated immutable ECR repository only.
- 02-runtime: dedicated VPC, two public subnets, Internet Gateway, ALB,
  restricted security groups, one Fargate task (0.25 vCPU / 0.5 GiB), two task
  definitions, a scoped execution role, CloudWatch logs (3-day retention),
  and a target HTTP 5xx alarm.
- No NAT Gateway, database, EC2 instance, Container Insights or remote-state
  infrastructure. The default VPC is not managed or changed.
- The existing local MCP and Bedrock agent are NOT modified or connected by
  this package. The alarm has no notification/action integration yet.

This is a disposable single-operator demo, not a highly available production
service. HTTP is restricted to one public IPv4 /32. Use synthetic requests
only. Fargate has a public IP to pull ECR layers and write logs; inbound 8080
is allowed only from the ALB security group.

## 1. Create the repository

Extract the ZIP into your existing cloud-ops-agent-mcp folder, then:

```bash
cd cloud-ops-terraform
terraform -chdir=01-registry init
terraform -chdir=01-registry fmt
terraform -chdir=01-registry validate
terraform -chdir=01-registry plan -out=registry.tfplan
terraform -chdir=01-registry apply registry.tfplan
```

The plan should contain exactly one resource to add, zero changes and zero
deletions. Review it before apply. Keep the generated .terraform.lock.hcl files.
Terraform 1.16.0 satisfies the declared version constraint; a patch upgrade is
not a prerequisite for this package.

## 2. Push the existing image

Ensure Docker Desktop is running. From cloud-ops-terraform:

```bash
bash push-image.sh
```

If the local image is missing, from cloud-ops-agent-mcp build it with:

```bash
docker build --platform linux/amd64 -t checkout-demo:local ./checkout-demo
```

The repository tag demo-v1 is immutable. If you change application code later,
use a new tag and update the data source in 02-runtime/main.tf. Runtime task
definitions pin the resolved image digest. APP_RELEASE v1/v2 refers to the
application settings, not separate Docker images.

## 3. Prepare and review runtime

From cloud-ops-terraform:

```bash
cp 02-runtime/terraform.tfvars.example 02-runtime/terraform.tfvars
```

Edit terraform.tfvars: replace the documentation IP with your current public
IPv4 address followed by /32. Keep demo_release = "v1". Do not use your
Mac's private Wi-Fi address. If a VPN changes your egress IP, update this value.

```bash
terraform -chdir=02-runtime init
terraform -chdir=02-runtime fmt
terraform -chdir=02-runtime validate
terraform -chdir=02-runtime plan -out=runtime.tfplan
```

Review the resources and account before applying. The runtime data lookup will
fail if ECR or the image does not exist; resolve that before starting runtime.

```bash
terraform -chdir=02-runtime apply runtime.tfplan
```

Runtime charges begin when resources are provisioned. ECS first-use accounts
may require permission for iam:CreateServiceLinkedRole. Deployment also needs
permissions for the listed resources and iam:PassRole for the execution role.
If apply fails, retain state; fix the specific error and re-plan, or destroy
the partial environment. Do not delete the directory to retry.

## 4. Verify the healthy release

```bash
DEMO_URL=$(terraform -chdir=02-runtime output -raw base_url)
curl --max-time 15 -i "$DEMO_URL/health"
curl --max-time 15 -i "$DEMO_URL/checkout"
aws logs tail /ecs/cloud-ops-demo/checkout-api --since 5m --region us-east-1
terraform -chdir=02-runtime output task_definitions
```

Expected: health 200 and checkout 200 with release v1. ALB DNS may take a short
time to resolve. A timeout can also mean your public IP no longer matches /32.

## 5. Demonstrate the fault and recover

In terraform.tfvars set demo_release = "v2", then:

```bash
terraform -chdir=02-runtime plan -out=fault.tfplan
terraform -chdir=02-runtime apply fault.tfplan
```

Wait for apply to finish. Repeat /health (200) and /checkout (500). Send checkout
requests for a couple of minutes to observe the CloudWatch 5xx alarm; metrics
are asynchronous. The log should contain CHECKOUT_CONFIG_ERROR and release v2.
This is a real response from a deliberately faulty configuration, not a real
database pool incident. Do not retain the old fixture's database diagnosis.

The ECS circuit breaker checks deployment health; the intentionally healthy
/health endpoint means this functional /checkout failure should not trigger
its rollback. The CloudWatch alarm is intentionally not attached to automatic
ECS rollback; the later human-approval flow will handle that decision.

For manual recovery, set demo_release = "v1" in terraform.tfvars, plan, review
and apply again. Confirm /checkout returns 200 with release v1 before declaring
recovery. Both revisions remain managed; use output task_definitions to identify
them. Parallel registration means numeric revision order does NOT identify
the healthy release. Never guess a rollback target using current_revision - 1.

Future direct rollback by the agent will change ECS outside Terraform. Update
terraform.tfvars to v1 and inspect a fresh plan before another apply; otherwise
Terraform may redeploy v2. No ignore_changes hides this difference.

## 6. Destroy after the rehearsal

From cloud-ops-terraform, use the same AWS credentials and keep both state files:

```bash
terraform -chdir=02-runtime plan -destroy -out=destroy-runtime.tfplan
terraform -chdir=02-runtime apply destroy-runtime.tfplan
terraform -chdir=02-runtime state list
terraform -chdir=01-registry plan -destroy -out=destroy-registry.tfplan
terraform -chdir=01-registry apply destroy-registry.tfplan
terraform -chdir=01-registry state list
```

Destroy runtime FIRST, then registry. These operations delete the demo logs
and ECR images too. Export any evidence you want to keep beforehand. Successful
destroy and no managed resource entries remaining confirm Terraform has removed
its tracked resources. Data-source-only entries, if any, are not billable resources.
This does not inventory unrelated account resources or erase past charges.

Setting desired_count to zero does NOT remove ALB, public IPv4, logs or ECR
storage costs. ALB, Fargate, public IPv4, CloudWatch and ECR usage can incur
charges. Destroy removes the tracked environment; billing display can lag.

Local state is deliberate for this one-person demo. Keep it until successful
cleanup; never commit state, tfplans, credentials or terraform.tfvars to Git.
The archive contains no state and creates no resources simply by extracting it.

## Validation and references

Prepared and reviewed statically. Terraform/provider validation and an AWS
apply were not run in the authoring environment; run init/validate/plan above
on the Mac before applying. No AWS changes were made while preparing this ZIP.

- https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/ecs_service
- https://docs.aws.amazon.com/AmazonECS/latest/developerguide/fargate-task-networking.html
- https://docs.aws.amazon.com/AmazonECS/latest/developerguide/deployment-circuit-breaker.html
