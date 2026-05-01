terraform {
  required_version = ">= 1.11.6, < 2.0.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 6.40.0, < 7.0.0"
    }

    tls = {
      source  = "hashicorp/tls"
      version = ">= 4.2.1, < 5.0.0"
    }
  }
}

provider "aws" {
  region = var.region

  default_tags {
    tags = merge(
      {
        ManagedBy   = "opentofu"
        Platform    = "mlops"
        Environment = var.environment
      },
      var.tags
    )
  }
}