data "aws_caller_identity" "current" {}
data "aws_region" "current" {}
data "aws_partition" "current" {}

# ── Prerequisite validation (replaces the PrereqCheck custom resource) ────────
# The CloudFormation template ran a Lambda-backed custom resource at deploy time
# to confirm Object Lock and Versioning are enabled on the target bucket. The
# AWS Terraform provider has no data source that reports a bucket's Object Lock
# or Versioning *state* (only managed resources exist), so this port keeps an
# equivalent read-only, deploy-time check as an `external` data source that
# shells out to the AWS CLI. It performs no writes.
#
# check_prereqs.sh prints {"object_lock":"...","versioning":"..."} and the
# preconditions on local_file-free null gate (see main.tf, null_resource.prereq)
# assert both are enabled before anything downstream is created.

data "external" "target_bucket_prereqs" {
  program = ["bash", "${path.module}/scripts/check_prereqs.sh", var.target_bucket]
}
