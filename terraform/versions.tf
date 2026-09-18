terraform {
  required_version = ">= 1.5.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.60.0"
    }
    archive = {
      source  = "hashicorp/archive"
      version = ">= 2.4.0"
    }
  }
}

provider "aws" {
  # Region is taken from the standard AWS provider configuration
  # (AWS_REGION / profile / shared config). The solution must be deployed in
  # the same Region as the target bucket, exactly as with the original
  # CloudFormation stack.
}
