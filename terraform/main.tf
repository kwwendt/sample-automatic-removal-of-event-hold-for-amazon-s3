# ─────────────────────────────────────────────────────────────────────────────
# Prerequisite / validation gate
#
# Replaces two things from the CloudFormation template:
#   1. The PrereqCheck custom resource — asserts Object Lock and Versioning are
#      enabled on the target bucket (via data.external.target_bucket_prereqs).
#   2. The CloudFormation `Rules` block — asserts that active release mode
#      (report_only = false) is not paired with safety_threshold = 0, which
#      would suspend every run and silently disable automatic release.
#
# Downstream resources that depend on the target bucket's readiness reference
# this null_resource so the checks run first. Resources that don't touch the
# target bucket (the solution bucket, queues, etc.) don't need the gate.
# ─────────────────────────────────────────────────────────────────────────────

resource "null_resource" "prereq" {
  lifecycle {
    precondition {
      condition     = data.external.target_bucket_prereqs.result.object_lock == "Enabled"
      error_message = "S3 Object Lock is not enabled on bucket '${var.target_bucket}'. Enable Object Lock and Versioning before deploying."
    }

    precondition {
      condition     = data.external.target_bucket_prereqs.result.versioning == "Enabled"
      error_message = "S3 Versioning is not enabled on bucket '${var.target_bucket}'. Enable Versioning before deploying."
    }

    precondition {
      condition     = var.report_only || var.safety_threshold > 0
      error_message = "When report_only is false (active release mode), safety_threshold must be greater than 0. A threshold of 0 would suspend every run regardless of eligible-version count, disabling automatic release."
    }
  }
}
