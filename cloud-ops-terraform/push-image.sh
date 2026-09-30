#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

demo_account=$(aws sts get-caller-identity --query Account --output text)
if [ "$demo_account" != "991731688366" ]; then
  echo "Wrong AWS account; expected the demo account. Stopping." >&2
  exit 1
fi
demo_repository=$(terraform -chdir=01-registry output -raw repository_url)
demo_registry=${demo_repository%%/*}
docker image inspect checkout-demo:local >/dev/null
demo_arch=$(docker image inspect checkout-demo:local --format '{{.Architecture}}')
if [ "$demo_arch" != "amd64" ]; then
  echo "Build checkout-demo:local for linux/amd64 before pushing." >&2
  exit 1
fi
aws ecr get-login-password --region us-east-1 |
  docker login --username AWS --password-stdin "$demo_registry"
docker tag checkout-demo:local "${demo_repository}:demo-v1"
docker push "${demo_repository}:demo-v1"
aws ecr describe-images --region us-east-1 \
  --repository-name cloud-ops-checkout --image-ids imageTag=demo-v1 \
  --query 'imageDetails[0].{Tags:imageTags,Digest:imageDigest}' --output json
