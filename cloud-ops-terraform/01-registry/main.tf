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

resource "aws_ecr_repository" "checkout" {
  name                 = "cloud-ops-checkout"
  image_tag_mutability = "IMMUTABLE"
  # Dedicated disposable demo: destroy also removes its images.
  force_delete = true
  encryption_configuration {
    encryption_type = "AES256"
  }
}

output "repository_url" {
  value = aws_ecr_repository.checkout.repository_url
}
