# ─────────────────────────────────────────────────────────────────────────────
# IAM roles and inline policies for the four pipeline Lambdas and the S3 Batch
# Operations service role. Each mirrors the corresponding CloudFormation role's
# inline Policies. The optional KMS policy is attached only when a CMK is given.
# ─────────────────────────────────────────────────────────────────────────────

data "aws_iam_policy_document" "lambda_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

# KMS grant reused by every execution role and the batchops role when a CMK is
# supplied. Rendered as JSON and attached conditionally.
data "aws_iam_policy_document" "kms_decrypt_generate" {
  count = local.has_kms_key ? 1 : 0
  statement {
    effect    = "Allow"
    actions   = ["kms:GenerateDataKey*", "kms:Decrypt"]
    resources = [var.kms_key_arn]
  }
}

locals {
  logs_arn_prefix = "arn:${local.partition}:logs:${local.region}:${local.account_id}:log-group:/aws/lambda"
  glue_catalog    = "arn:${local.partition}:glue:${local.region}:${local.account_id}:catalog"
  glue_db_arn     = "arn:${local.partition}:glue:${local.region}:${local.account_id}:database/${local.glue_database_name}"
  glue_tbl_prefix = "arn:${local.partition}:glue:${local.region}:${local.account_id}:table/${local.glue_database_name}"
  athena_wg_arn   = "arn:${local.partition}:athena:${local.region}:${local.account_id}:workgroup/${local.athena_workgroup}"
  job_arn         = "arn:${local.partition}:s3:*:${local.account_id}:job/*"
}

# ══════════════════════════════════════════════════════════════════════════════
# StartQuery role
# ══════════════════════════════════════════════════════════════════════════════
resource "aws_iam_role" "startquery" {
  name               = "${var.name_prefix}-startquery-role"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
  tags               = local.tags
}

data "aws_iam_policy_document" "startquery" {
  statement {
    sid    = "AthenaQueryExecution"
    effect = "Allow"
    actions = [
      "athena:StartQueryExecution",
      "athena:GetQueryExecution",
      "athena:GetQueryResults",
    ]
    resources = [local.athena_wg_arn]
  }

  statement {
    sid     = "GlueTableAccess"
    effect  = "Allow"
    actions = ["glue:GetDatabase", "glue:GetTable", "glue:GetPartitions"]
    resources = [
      local.glue_catalog,
      local.glue_db_arn,
      "${local.glue_tbl_prefix}/${local.glue_table_name}",
      "${local.glue_tbl_prefix}/${local.previous_manifest_table_name}",
    ]
  }

  statement {
    sid     = "S3InventoryReadGet"
    effect  = "Allow"
    actions = ["s3:GetObject"]
    resources = [
      "${aws_s3_bucket.solution.arn}/inventory/*",
      "${aws_s3_bucket.solution.arn}/manifests/previous/*",
    ]
  }

  statement {
    sid       = "S3InventoryReadList"
    effect    = "Allow"
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.solution.arn]
    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values   = ["inventory/*", "manifests/parts/*", "manifests/previous/*", "athena-results/*"]
    }
  }

  statement {
    sid       = "S3GetBucketLocation"
    effect    = "Allow"
    actions   = ["s3:GetBucketLocation"]
    resources = [aws_s3_bucket.solution.arn]
  }

  statement {
    sid     = "S3UnloadWrite"
    effect  = "Allow"
    actions = ["s3:PutObject", "s3:GetObject"]
    resources = [
      "${aws_s3_bucket.solution.arn}/manifests/parts/*",
      "${aws_s3_bucket.solution.arn}/athena-results/*",
    ]
  }

  statement {
    sid       = "CloudWatchLogsWrite"
    effect    = "Allow"
    actions   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${local.logs_arn_prefix}/${var.name_prefix}-startquery:*"]
  }

  statement {
    sid       = "DLQSendMessage"
    effect    = "Allow"
    actions   = ["sqs:SendMessage"]
    resources = [aws_sqs_queue.pipeline_dlq.arn]
  }
}

resource "aws_iam_role_policy" "startquery" {
  name   = "startquery"
  role   = aws_iam_role.startquery.id
  policy = data.aws_iam_policy_document.startquery.json
}

resource "aws_iam_role_policy" "startquery_kms" {
  count  = local.has_kms_key ? 1 : 0
  name   = "kms"
  role   = aws_iam_role.startquery.id
  policy = data.aws_iam_policy_document.kms_decrypt_generate[0].json
}

# ══════════════════════════════════════════════════════════════════════════════
# ManifestMaker role
# ══════════════════════════════════════════════════════════════════════════════
resource "aws_iam_role" "manifestmaker" {
  name               = "${var.name_prefix}-manifestmaker-role"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
  tags               = local.tags
}

data "aws_iam_policy_document" "manifestmaker" {
  statement {
    sid       = "S3ManifestPartsRead"
    effect    = "Allow"
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.solution.arn}/manifests/parts/*"]
  }

  statement {
    sid       = "S3ManifestPartsList"
    effect    = "Allow"
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.solution.arn]
    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values   = ["manifests/parts/*"]
    }
  }

  statement {
    sid     = "S3ManifestCombinedWrite"
    effect  = "Allow"
    actions = ["s3:PutObject", "s3:GetObject", "s3:AbortMultipartUpload"]
    resources = [
      "${aws_s3_bucket.solution.arn}/manifests/combined/*/*/manifest.csv",
      "${aws_s3_bucket.solution.arn}/manifests/combined/*/_manifests_ready.json",
      "${aws_s3_bucket.solution.arn}/diagnostics/withheld-candidates/*",
    ]
  }

  statement {
    sid       = "CloudWatchLogsWrite"
    effect    = "Allow"
    actions   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${local.logs_arn_prefix}/${var.name_prefix}-manifestmaker:*"]
  }

  statement {
    sid       = "DLQSendMessage"
    effect    = "Allow"
    actions   = ["sqs:SendMessage"]
    resources = [aws_sqs_queue.pipeline_dlq.arn]
  }
}

resource "aws_iam_role_policy" "manifestmaker" {
  name   = "manifestmaker"
  role   = aws_iam_role.manifestmaker.id
  policy = data.aws_iam_policy_document.manifestmaker.json
}

resource "aws_iam_role_policy" "manifestmaker_kms" {
  count  = local.has_kms_key ? 1 : 0
  name   = "kms"
  role   = aws_iam_role.manifestmaker.id
  policy = data.aws_iam_policy_document.kms_decrypt_generate[0].json
}

# ══════════════════════════════════════════════════════════════════════════════
# CreateJob role
# ══════════════════════════════════════════════════════════════════════════════
resource "aws_iam_role" "createjob" {
  name               = "${var.name_prefix}-createjob-role"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
  tags               = local.tags
}

data "aws_iam_policy_document" "createjob" {
  statement {
    sid       = "S3CreateJob"
    effect    = "Allow"
    actions   = ["s3:CreateJob", "s3:PutJobTagging"]
    resources = [local.job_arn]
  }

  statement {
    sid       = "PassBatchRole"
    effect    = "Allow"
    actions   = ["iam:PassRole"]
    resources = [aws_iam_role.batchops.arn]
    condition {
      test     = "StringEquals"
      variable = "iam:PassedToService"
      values   = ["s3.amazonaws.com"]
    }
  }

  statement {
    sid     = "S3ManifestRead"
    effect  = "Allow"
    actions = ["s3:GetObject"]
    resources = [
      "${aws_s3_bucket.solution.arn}/manifests/combined/*",
      "${aws_s3_bucket.solution.arn}/athena-results/*",
    ]
  }

  statement {
    sid     = "S3SummaryWrite"
    effect  = "Allow"
    actions = ["s3:PutObject"]
    resources = [
      "${aws_s3_bucket.solution.arn}/manifests/combined/*/_job_summary.json",
      "${aws_s3_bucket.solution.arn}/manifests/combined/*/_job_summaries/*",
      "${aws_s3_bucket.solution.arn}/manifests/combined/*/_createjob_lock.json",
      "${aws_s3_bucket.solution.arn}/manifests/combined/*/_active_jobs.json",
    ]
  }

  statement {
    sid       = "S3PreviousManifestWrite"
    effect    = "Allow"
    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.solution.arn}/manifests/previous/*"]
  }

  statement {
    sid       = "S3ManifestList"
    effect    = "Allow"
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.solution.arn]
    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values   = ["manifests/combined/*"]
    }
  }

  statement {
    sid       = "SNSPublish"
    effect    = "Allow"
    actions   = ["sns:Publish"]
    resources = [aws_sns_topic.this.arn]
  }

  statement {
    sid       = "AthenaVerifyCountQuery"
    effect    = "Allow"
    actions   = ["athena:GetQueryExecution", "athena:GetQueryResults"]
    resources = [local.athena_wg_arn]
  }

  statement {
    sid       = "CloudWatchLogsWrite"
    effect    = "Allow"
    actions   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${local.logs_arn_prefix}/${var.name_prefix}-createjob:*"]
  }

  statement {
    sid       = "DLQSendMessage"
    effect    = "Allow"
    actions   = ["sqs:SendMessage"]
    resources = [aws_sqs_queue.pipeline_dlq.arn]
  }
}

resource "aws_iam_role_policy" "createjob" {
  name   = "createjob"
  role   = aws_iam_role.createjob.id
  policy = data.aws_iam_policy_document.createjob.json
}

resource "aws_iam_role_policy" "createjob_kms" {
  count  = local.has_kms_key ? 1 : 0
  name   = "kms"
  role   = aws_iam_role.createjob.id
  policy = data.aws_iam_policy_document.kms_decrypt_generate[0].json
}

# ══════════════════════════════════════════════════════════════════════════════
# JobCompletion role
# ══════════════════════════════════════════════════════════════════════════════
resource "aws_iam_role" "jobcompletion" {
  name               = "${var.name_prefix}-jobcompletion-role"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
  tags               = local.tags
}

data "aws_iam_policy_document" "jobcompletion" {
  statement {
    sid       = "S3DescribeJob"
    effect    = "Allow"
    actions   = ["s3:DescribeJob", "s3:GetJobTagging"]
    resources = [local.job_arn]
  }

  statement {
    sid       = "S3ManifestRead"
    effect    = "Allow"
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.solution.arn}/manifests/combined/*"]
  }

  statement {
    sid       = "S3ManifestList"
    effect    = "Allow"
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.solution.arn]
    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values   = ["manifests/combined/*"]
    }
  }

  statement {
    sid     = "S3SummaryWrite"
    effect  = "Allow"
    actions = ["s3:PutObject"]
    resources = [
      "${aws_s3_bucket.solution.arn}/manifests/combined/*/_active_jobs.json",
      "${aws_s3_bucket.solution.arn}/manifests/combined/*/_job_summary.json",
      "${aws_s3_bucket.solution.arn}/manifests/combined/*/_job_summaries/*",
      "${aws_s3_bucket.solution.arn}/manifests/combined/*/_completion_sent.json",
    ]
  }

  statement {
    sid       = "SNSPublish"
    effect    = "Allow"
    actions   = ["sns:Publish"]
    resources = [aws_sns_topic.this.arn]
  }

  statement {
    sid       = "CloudWatchLogsWrite"
    effect    = "Allow"
    actions   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${local.logs_arn_prefix}/${var.name_prefix}-jobcompletion:*"]
  }

  statement {
    sid       = "DLQSendMessage"
    effect    = "Allow"
    actions   = ["sqs:SendMessage"]
    resources = [aws_sqs_queue.pipeline_dlq.arn]
  }
}

resource "aws_iam_role_policy" "jobcompletion" {
  name   = "jobcompletion"
  role   = aws_iam_role.jobcompletion.id
  policy = data.aws_iam_policy_document.jobcompletion.json
}

resource "aws_iam_role_policy" "jobcompletion_kms" {
  count  = local.has_kms_key ? 1 : 0
  name   = "kms"
  role   = aws_iam_role.jobcompletion.id
  policy = data.aws_iam_policy_document.kms_decrypt_generate[0].json
}

# ══════════════════════════════════════════════════════════════════════════════
# S3 Batch Operations service role
# ══════════════════════════════════════════════════════════════════════════════
data "aws_iam_policy_document" "batchops_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["batchoperations.s3.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "batchops" {
  name               = "${var.name_prefix}-batchops-role"
  assume_role_policy = data.aws_iam_policy_document.batchops_assume.json
  tags               = local.tags
}

data "aws_iam_policy_document" "batchops" {
  # Release the event hold. Scoped to target bucket + prefix. No
  # BypassGovernanceRetention — releasing a hold never shortens protection.
  statement {
    sid       = "ReleaseEventHold"
    effect    = "Allow"
    actions   = ["s3:PutObjectRetention"]
    resources = [local.target_object_arn]
  }

  statement {
    sid       = "ReadObjectLockConfig"
    effect    = "Allow"
    actions   = ["s3:GetBucketObjectLockConfiguration"]
    resources = ["arn:${local.partition}:s3:::${var.target_bucket}"]
  }

  statement {
    sid       = "ReadManifest"
    effect    = "Allow"
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.solution.arn}/manifests/combined/*"]
  }

  statement {
    sid       = "WriteCompletionReport"
    effect    = "Allow"
    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.solution.arn}/reports/*"]
  }
}

resource "aws_iam_role_policy" "batchops" {
  name   = "batchops"
  role   = aws_iam_role.batchops.id
  policy = data.aws_iam_policy_document.batchops.json
}

resource "aws_iam_role_policy" "batchops_kms" {
  count  = local.has_kms_key ? 1 : 0
  name   = "kms"
  role   = aws_iam_role.batchops.id
  policy = data.aws_iam_policy_document.kms_decrypt_generate[0].json
}
