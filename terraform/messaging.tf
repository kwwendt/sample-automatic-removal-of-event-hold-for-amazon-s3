# ─────────────────────────────────────────────────────────────────────────────
# Pipeline dead-letter queue (shared by the four event-driven pipeline Lambdas).
# ─────────────────────────────────────────────────────────────────────────────

resource "aws_sqs_queue" "pipeline_dlq" {
  name                      = "${var.name_prefix}-pipeline-dlq"
  message_retention_seconds = 1209600 # 14 days (maximum)
  kms_master_key_id         = local.has_kms_key ? var.kms_key_arn : null
  tags                      = local.tags
}

# Deny-only policy: grants nothing, so the pipeline Lambdas keep writing to the
# DLQ through the sqs:SendMessage grant in their own execution roles. Blocks any
# SQS operation on this queue that did not arrive over TLS.
data "aws_iam_policy_document" "pipeline_dlq" {
  statement {
    sid       = "DenyInsecureTransport"
    effect    = "Deny"
    actions   = ["sqs:*"]
    resources = [aws_sqs_queue.pipeline_dlq.arn]
    principals {
      type        = "AWS"
      identifiers = ["*"]
    }
    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

resource "aws_sqs_queue_policy" "pipeline_dlq" {
  queue_url = aws_sqs_queue.pipeline_dlq.id
  policy    = data.aws_iam_policy_document.pipeline_dlq.json
}

# ─────────────────────────────────────────────────────────────────────────────
# SNS topic — run summaries, confirmation prompts, failure notifications.
# No subscription is provisioned here; operators subscribe after deployment.
# ─────────────────────────────────────────────────────────────────────────────

resource "aws_sns_topic" "this" {
  name              = "${var.name_prefix}-topic"
  display_name      = "Auto Remove Event Hold — ${var.name_prefix}"
  kms_master_key_id = local.has_kms_key ? var.kms_key_arn : null
  tags              = local.tags
}

data "aws_iam_policy_document" "sns_topic" {
  statement {
    sid     = "AllowCreateJobPublish"
    effect  = "Allow"
    actions = ["sns:Publish"]
    principals {
      type = "AWS"
      identifiers = [
        aws_iam_role.createjob.arn,
        aws_iam_role.jobcompletion.arn,
      ]
    }
    resources = [aws_sns_topic.this.arn]
  }

  # Required: setting any topic policy replaces SNS's default, so CloudWatch
  # Alarms must be re-granted publish or JobCompletionMissingRecordAlarm and
  # PipelineDLQAlarm can't deliver. Scoped to this stack's own alarms.
  statement {
    sid     = "AllowCloudWatchAlarmPublish"
    effect  = "Allow"
    actions = ["sns:Publish"]
    principals {
      type        = "Service"
      identifiers = ["cloudwatch.amazonaws.com"]
    }
    resources = [aws_sns_topic.this.arn]
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }
    condition {
      test     = "ArnLike"
      variable = "aws:SourceArn"
      values   = ["arn:${local.partition}:cloudwatch:${local.region}:${local.account_id}:alarm:${var.name_prefix}-*"]
    }
  }

  statement {
    sid       = "DenyInsecureTransport"
    effect    = "Deny"
    actions   = ["sns:Publish"]
    resources = [aws_sns_topic.this.arn]
    principals {
      type        = "AWS"
      identifiers = ["*"]
    }
    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

resource "aws_sns_topic_policy" "this" {
  arn    = aws_sns_topic.this.arn
  policy = data.aws_iam_policy_document.sns_topic.json
}
