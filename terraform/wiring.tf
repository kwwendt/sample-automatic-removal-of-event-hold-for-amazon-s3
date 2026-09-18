# ─────────────────────────────────────────────────────────────────────────────
# Event wiring: S3 notifications, EventBridge rule, and the target-bucket S3
# Inventory configuration. Replaces the SetupNotifications and InventoryConfig
# custom resources with native Terraform resources.
# ─────────────────────────────────────────────────────────────────────────────

# ── S3 event notifications on the SolutionBucket ──────────────────────────────
# The template used a custom resource here to break a CloudFormation circular
# dependency; Terraform has no such cycle, so this is native. depends_on the
# permissions so S3 can validate the Lambda targets at create time.
resource "aws_s3_bucket_notification" "solution" {
  bucket = aws_s3_bucket.solution.id

  lambda_function {
    id                  = "InventoryManifestToStartQuery"
    lambda_function_arn = aws_lambda_function.startquery.arn
    events              = ["s3:ObjectCreated:*"]
    filter_prefix       = "inventory/"
    filter_suffix       = "manifest.json"
  }

  lambda_function {
    id                  = "QueryCompleteToManifestMaker"
    lambda_function_arn = aws_lambda_function.manifestmaker.arn
    events              = ["s3:ObjectCreated:*"]
    filter_prefix       = "manifests/parts/"
    filter_suffix       = "_query_complete.json"
  }

  lambda_function {
    id                  = "ManifestsReadyToCreateJob"
    lambda_function_arn = aws_lambda_function.createjob.arn
    events              = ["s3:ObjectCreated:*"]
    filter_prefix       = "manifests/combined/"
    filter_suffix       = "_manifests_ready.json"
  }

  depends_on = [
    aws_lambda_permission.startquery_s3,
    aws_lambda_permission.manifestmaker_s3,
    aws_lambda_permission.createjob_s3,
  ]
}

# ── S3 Inventory configuration on the target bucket ───────────────────────────
# Replaces the InventoryConfig custom resource. This is NOT done with the native
# aws_s3_bucket_inventory resource on purpose: that resource's optional_fields
# validation rejects "ObjectLockEventHoldStatus", the single field the whole
# eligibility query depends on (object_lock_event_hold_status = 'ON'). The
# original template hit the same wall — CloudFormation's AWS::S3::Bucket
# inventory schema also lacks it — which is exactly why it configured the
# inventory imperatively through a Lambda calling put_bucket_inventory_configuration.
#
# The faithful Terraform equivalent is a local-exec provisioner that calls the
# same S3 API via the AWS CLI, with a destroy-time provisioner that removes only
# this configuration (matching the custom resource's create/delete behavior).
# Requires the AWS CLI on the machine running terraform apply. Triggers force a
# re-apply when any input to the configuration changes.
resource "null_resource" "target_bucket_inventory" {
  triggers = {
    target_bucket      = var.target_bucket
    solution_bucket    = aws_s3_bucket.solution.id
    inventory_id       = var.name_prefix
    prefix             = var.prefix
    detection_schedule = var.detection_schedule
    account_id         = local.account_id
    partition          = local.partition
  }

  provisioner "local-exec" {
    command = "${path.module}/scripts/put_inventory.sh"
    environment = {
      TARGET_BUCKET      = self.triggers.target_bucket
      SOLUTION_BUCKET    = self.triggers.solution_bucket
      INVENTORY_ID       = self.triggers.inventory_id
      PREFIX             = self.triggers.prefix
      DETECTION_SCHEDULE = self.triggers.detection_schedule
      ACCOUNT_ID         = self.triggers.account_id
      PARTITION          = self.triggers.partition
    }
  }

  # Remove only this stack's inventory configuration on destroy.
  provisioner "local-exec" {
    when       = destroy
    on_failure = continue
    command    = "aws s3api delete-bucket-inventory-configuration --bucket '${self.triggers.target_bucket}' --id '${self.triggers.inventory_id}'"
  }

  depends_on = [
    null_resource.prereq,
    aws_s3_bucket_policy.solution,
  ]
}

# ── EventBridge rule: terminal Batch Operations job status -> JobCompletion ────
resource "aws_cloudwatch_event_rule" "job_completion" {
  name        = "${var.name_prefix}-jobcompletion-rule"
  description = "Matches terminal S3 Batch Operations job status-change events (delivered via CloudTrail to the default EventBridge bus) and invokes JobCompletion."
  tags        = local.tags

  event_pattern = jsonencode({
    source      = ["aws.s3"]
    detail-type = ["AWS Service Event via CloudTrail"]
    detail = {
      eventSource = ["s3.amazonaws.com"]
      eventName   = ["JobStatusChanged"]
      serviceEventDetails = {
        status = ["Complete", "Cancelled", "Failed"]
      }
    }
  })
}

resource "aws_cloudwatch_event_target" "job_completion" {
  rule      = aws_cloudwatch_event_rule.job_completion.name
  target_id = "JobCompletionFunctionTarget"
  arn       = aws_lambda_function.jobcompletion.arn
}
