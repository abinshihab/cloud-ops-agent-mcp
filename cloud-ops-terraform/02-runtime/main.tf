terraform {
  required_version = ">= 1.6.0, < 2.0.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }
}

provider "aws" {
  region              = "us-east-1"
  allowed_account_ids = ["991731688366"]
  default_tags {
    tags = { Project = "cloud-ops-demo", ManagedBy = "Terraform" }
  }
}

variable "allowed_cidr" {
  type        = string
  description = "Your public IPv4 address with /32; only this address can reach the ALB."
  validation {
    condition     = can(cidrnetmask(var.allowed_cidr)) && endswith(var.allowed_cidr, "/32")
    error_message = "Use one IPv4 address with /32."
  }
}

variable "demo_release" {
  type    = string
  default = "v1"
  validation {
    condition     = contains(["v1", "v2"], var.demo_release)
    error_message = "Choose v1 (healthy) or v2 (intentional checkout fault)."
  }
}

data "aws_availability_zones" "available" {
  state = "available"
  filter {
    name   = "zone-type"
    values = ["availability-zone"]
  }
}

data "aws_ecr_repository" "checkout" {
  name = "cloud-ops-checkout"
}

# Fails at plan time if the image has not been pushed yet.
data "aws_ecr_image" "checkout" {
  repository_name = data.aws_ecr_repository.checkout.name
  image_tag       = "demo-v1"
}

locals {
  name  = "cloud-ops-demo"
  image = "${data.aws_ecr_repository.checkout.repository_url}@${data.aws_ecr_image.checkout.image_digest}"
}

resource "aws_vpc" "demo" {
  cidr_block           = "10.77.0.0/16"
  enable_dns_support   = true
  enable_dns_hostnames = true
  tags                 = { Name = local.name }
}

resource "aws_subnet" "public" {
  count             = 2
  vpc_id            = aws_vpc.demo.id
  cidr_block        = cidrsubnet(aws_vpc.demo.cidr_block, 8, count.index + 1)
  availability_zone = data.aws_availability_zones.available.names[count.index]
  tags              = { Name = "${local.name}-public-${count.index + 1}" }
}

resource "aws_internet_gateway" "demo" {
  vpc_id = aws_vpc.demo.id
  tags   = { Name = local.name }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.demo.id
  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.demo.id
  }
  tags = { Name = "${local.name}-public" }
}

resource "aws_route_table_association" "public" {
  count          = 2
  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}

resource "aws_security_group" "alb" {
  name   = "${local.name}-alb"
  vpc_id = aws_vpc.demo.id
}

resource "aws_security_group" "task" {
  name   = "${local.name}-task"
  vpc_id = aws_vpc.demo.id
}

resource "aws_vpc_security_group_ingress_rule" "your_ip" {
  security_group_id = aws_security_group.alb.id
  cidr_ipv4         = var.allowed_cidr
  ip_protocol       = "tcp"
  from_port         = 80
  to_port           = 80
}

resource "aws_vpc_security_group_egress_rule" "alb_to_task" {
  security_group_id            = aws_security_group.alb.id
  referenced_security_group_id = aws_security_group.task.id
  ip_protocol                  = "tcp"
  from_port                    = 8080
  to_port                      = 8080
}

resource "aws_vpc_security_group_ingress_rule" "task_from_alb" {
  security_group_id            = aws_security_group.task.id
  referenced_security_group_id = aws_security_group.alb.id
  ip_protocol                  = "tcp"
  from_port                    = 8080
  to_port                      = 8080
}

resource "aws_vpc_security_group_egress_rule" "task_https" {
  security_group_id = aws_security_group.task.id
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
}

resource "aws_lb" "demo" {
  name               = local.name
  internal           = false
  load_balancer_type = "application"
  security_groups    = [aws_security_group.alb.id]
  subnets            = aws_subnet.public[*].id
  depends_on         = [aws_route_table_association.public]
}

resource "aws_lb_target_group" "checkout" {
  name                 = "${local.name}-checkout"
  port                 = 8080
  protocol             = "HTTP"
  target_type          = "ip"
  vpc_id               = aws_vpc.demo.id
  deregistration_delay = 10
  health_check {
    path                = "/health"
    matcher             = "200"
    interval            = 15
    timeout             = 5
    healthy_threshold   = 2
    unhealthy_threshold = 2
  }
}

resource "aws_lb_listener" "http" {
  load_balancer_arn = aws_lb.demo.arn
  port              = 80
  protocol          = "HTTP"
  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.checkout.arn
  }
}

resource "aws_cloudwatch_log_group" "checkout" {
  name              = "/ecs/cloud-ops-demo/checkout-api"
  retention_in_days = 3
}

resource "aws_iam_role" "execution" {
  name = "${local.name}-execution"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "execution" {
  name = "pull-demo-image-and-write-logs"
  role = aws_iam_role.execution.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["ecr:GetAuthorizationToken"]
        Resource = "*"
      },
      {
        Effect   = "Allow"
        Action   = ["ecr:BatchCheckLayerAvailability", "ecr:GetDownloadUrlForLayer", "ecr:BatchGetImage"]
        Resource = data.aws_ecr_repository.checkout.arn
      },
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "${aws_cloudwatch_log_group.checkout.arn}:*"
      }
    ]
  })
}

resource "aws_ecs_cluster" "demo" {
  name = local.name
  setting {
    name  = "containerInsights"
    value = "disabled"
  }
}

# Both revisions remain managed and available for explicit rollback.
resource "aws_ecs_task_definition" "checkout" {
  for_each                 = { v1 = "healthy", v2 = "faulty" }
  family                   = "cloud-ops-checkout"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = "256"
  memory                   = "512"
  execution_role_arn       = aws_iam_role.execution.arn
  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }
  container_definitions = jsonencode([{
    name         = "checkout-api"
    image        = local.image
    essential    = true
    portMappings = [{ containerPort = 8080, hostPort = 8080, protocol = "tcp" }]
    environment = [
      { name = "APP_RELEASE", value = each.key },
      { name = "APP_MODE", value = each.value }
    ]
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = aws_cloudwatch_log_group.checkout.name
        "awslogs-region"        = "us-east-1"
        "awslogs-stream-prefix" = "ecs"
      }
    }
  }])
}

resource "aws_ecs_service" "checkout" {
  name                               = "checkout-api"
  cluster                            = aws_ecs_cluster.demo.id
  task_definition                    = aws_ecs_task_definition.checkout[var.demo_release].arn
  desired_count                      = 1
  launch_type                        = "FARGATE"
  wait_for_steady_state              = true
  health_check_grace_period_seconds  = 45
  deployment_minimum_healthy_percent = 100
  deployment_maximum_percent         = 200
  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }
  network_configuration {
    subnets          = aws_subnet.public[*].id
    security_groups  = [aws_security_group.task.id]
    assign_public_ip = true
  }
  load_balancer {
    target_group_arn = aws_lb_target_group.checkout.arn
    container_name   = "checkout-api"
    container_port   = 8080
  }
  depends_on = [
    aws_lb_listener.http, aws_iam_role_policy.execution,
    aws_route_table_association.public,
    aws_vpc_security_group_egress_rule.task_https,
    aws_vpc_security_group_ingress_rule.task_from_alb,
    aws_vpc_security_group_egress_rule.alb_to_task
  ]
}

resource "aws_cloudwatch_metric_alarm" "checkout_5xx" {
  alarm_name          = "cloud-ops-demo-checkout-5xx"
  alarm_description   = "Demo target returned at least one HTTP 5xx in one minute. No remediation is attached."
  namespace           = "AWS/ApplicationELB"
  metric_name         = "HTTPCode_Target_5XX_Count"
  statistic           = "Sum"
  period              = 60
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  dimensions = {
    LoadBalancer = aws_lb.demo.arn_suffix
    TargetGroup  = aws_lb_target_group.checkout.arn_suffix
  }
}

output "base_url" {
  value = "http://${aws_lb.demo.dns_name}"
}
output "cluster_name" {
  value = aws_ecs_cluster.demo.name
}
output "service_name" {
  value = aws_ecs_service.checkout.name
}
output "log_group" {
  value = aws_cloudwatch_log_group.checkout.name
}
output "task_definitions" {
  value = { for release, task in aws_ecs_task_definition.checkout : release => task.arn }
}
output "alarm_name" {
  value = aws_cloudwatch_metric_alarm.checkout_5xx.alarm_name
}
