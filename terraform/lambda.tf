# ─────────────────────────────────────────────────────────────────────────────
# Lambda packaging, log groups, functions, and invoke permissions for the four
# pipeline functions. Source lives under functions/<name>/index.py and is zipped
# by the archive provider at plan time.
# ─────────────────────────────────────────────────────────────────────────────

data "archive_file" "startquery" {
  type        = "zip"
  source_file = "${path.module}/functions/startquery/index.py"
  output_path = "${path.module}/build/startquery.zip"
}

data "archive_file" "manifestmaker" {
  type        = "zip"
  source_file = "${path.module}/functions/manifestmaker/index.py"
  output_path = "${path.module}/build/manifestmaker.zip"
}

data "archive_file" "createjob" {
  type        = "zip"
  source_file = "${path.module}/functions/createjob/index.py"
  output_path = "${path.module}/build/createjob.zip"
}

# ── boto3 layer for CreateJob ─────────────────────────────────────────────────
# The managed runtime's bundled botocore lacks the EventHold parameter on the
# s3control retention shape, so CreateJob withholds GOVERNANCE releases
# ("event_hold_unsupported_by_runtime"). This layer supplies a newer boto3 ahead
# of the runtime SDK so create_job accepts EventHold. Only CreateJob needs it.
#
# scripts/build_boto3_layer.sh pip-installs boto3 into build/boto3-layer/python/;
# the archive_file below zips it once the build has run.
resource "null_resource" "boto3_layer_build" {
  triggers = {
    boto3_version = var.boto3_layer_version
    build_script  = filesha256("${path.module}/scripts/build_boto3_layer.sh")
  }

  provisioner "local-exec" {
    command = "${path.module}/scripts/build_boto3_layer.sh"
    environment = {
      BOTO3_VERSION   = var.boto3_layer_version
      LAYER_BUILD_DIR = "${path.module}/build/boto3-layer"
    }
  }
}

data "archive_file" "boto3_layer" {
  type        = "zip"
  source_dir  = "${path.module}/build/boto3-layer"
  output_path = "${path.module}/build/boto3-layer.zip"

  depends_on = [null_resource.boto3_layer_build]
}

resource "aws_lambda_layer_version" "boto3" {
  layer_name          = "${var.name_prefix}-boto3"
  description         = "boto3 ${var.boto3_layer_version} (adds s3control EventHold support absent from the runtime SDK)."
  filename            = data.archive_file.boto3_layer.output_path
  source_code_hash    = data.archive_file.boto3_layer.output_base64sha256
  compatible_runtimes = [var.lambda_runtime]
}

data "archive_file" "jobcompletion" {
  type        = "zip"
  source_file = "${path.module}/functions/jobcompletion/index.py"
  output_path = "${path.module}/build/jobcompletion.zip"
}

# ── Log groups ────────────────────────────────────────────────────────────────
# Explicit, so the runtime never auto-creates an unmanaged Never-Expire group.
resource "aws_cloudwatch_log_group" "startquery" {
  name              = "/aws/lambda/${var.name_prefix}-startquery"
  retention_in_days = var.log_retention_days
  kms_key_id        = local.has_kms_key ? var.kms_key_arn : null
  tags              = local.tags
}

resource "aws_cloudwatch_log_group" "manifestmaker" {
  name              = "/aws/lambda/${var.name_prefix}-manifestmaker"
  retention_in_days = var.log_retention_days
  kms_key_id        = local.has_kms_key ? var.kms_key_arn : null
  tags              = local.tags
}

resource "aws_cloudwatch_log_group" "createjob" {
  name              = "/aws/lambda/${var.name_prefix}-createjob"
  retention_in_days = var.log_retention_days
  kms_key_id        = local.has_kms_key ? var.kms_key_arn : null
  tags              = local.tags
}

resource "aws_cloudwatch_log_group" "jobcompletion" {
  name              = "/aws/lambda/${var.name_prefix}-jobcompletion"
  retention_in_days = var.log_retention_days
  kms_key_id        = local.has_kms_key ? var.kms_key_arn : null
  tags              = local.tags
}

# ── StartQuery ────────────────────────────────────────────────────────────────
resource "aws_lambda_function" "startquery" {
  function_name    = "${var.name_prefix}-startquery"
  description      = "Counts eligible versions, writes per-mode manifests, and emits completion metadata."
  role             = aws_iam_role.startquery.arn
  runtime          = var.lambda_runtime
  handler          = "index.handler"
  timeout          = 900
  filename         = data.archive_file.startquery.output_path
  source_code_hash = data.archive_file.startquery.output_base64sha256
  tags             = local.tags

  dead_letter_config {
    target_arn = aws_sqs_queue.pipeline_dlq.arn
  }

  environment {
    variables = {
      SOLUTION_BUCKET         = aws_s3_bucket.solution.id
      GLUE_DATABASE           = local.glue_database_name
      GLUE_TABLE              = local.glue_table_name
      PREVIOUS_MANIFEST_TABLE = local.previous_manifest_table_name
      ATHENA_WORKGROUP        = local.athena_workgroup
      TARGET_BUCKET           = var.target_bucket
      INVENTORY_ID            = var.name_prefix
      RELEASE_MODE            = var.release_mode
    }
  }

  depends_on = [
    aws_iam_role_policy.startquery,
    aws_cloudwatch_log_group.startquery,
  ]
}

# ── ManifestMaker ─────────────────────────────────────────────────────────────
resource "aws_lambda_function" "manifestmaker" {
  function_name    = "${var.name_prefix}-manifestmaker"
  description      = "Combines Athena output parts into one Batch Operations manifest per lock mode."
  role             = aws_iam_role.manifestmaker.arn
  runtime          = var.lambda_runtime
  handler          = "index.handler"
  timeout          = 900
  filename         = data.archive_file.manifestmaker.output_path
  source_code_hash = data.archive_file.manifestmaker.output_base64sha256
  tags             = local.tags

  dead_letter_config {
    target_arn = aws_sqs_queue.pipeline_dlq.arn
  }

  environment {
    variables = {
      SOLUTION_BUCKET = aws_s3_bucket.solution.id
    }
  }

  depends_on = [
    aws_iam_role_policy.manifestmaker,
    aws_cloudwatch_log_group.manifestmaker,
  ]
}

# ── CreateJob ─────────────────────────────────────────────────────────────────
resource "aws_lambda_function" "createjob" {
  function_name    = "${var.name_prefix}-createjob"
  description      = "Verifies eligibility counts and creates only approved S3 Batch Operations (S3PutObjectRetention) jobs for release."
  role             = aws_iam_role.createjob.arn
  runtime          = var.lambda_runtime
  handler          = "index.handler"
  timeout          = 300
  filename         = data.archive_file.createjob.output_path
  source_code_hash = data.archive_file.createjob.output_base64sha256
  layers           = [aws_lambda_layer_version.boto3.arn]
  tags             = local.tags

  dead_letter_config {
    target_arn = aws_sqs_queue.pipeline_dlq.arn
  }

  environment {
    variables = {
      SOLUTION_BUCKET           = aws_s3_bucket.solution.id
      REPORT_ONLY               = var.report_only ? "true" : "false"
      SAFETY_THRESHOLD          = tostring(var.safety_threshold)
      SNS_TOPIC_ARN             = aws_sns_topic.this.arn
      BATCH_OPERATIONS_ROLE_ARN = aws_iam_role.batchops.arn
      TARGET_BUCKET             = var.target_bucket
      ATHENA_WORKGROUP          = local.athena_workgroup
      STACK_NAME                = var.name_prefix
    }
  }

  depends_on = [
    aws_iam_role_policy.createjob,
    aws_cloudwatch_log_group.createjob,
  ]
}

# ── JobCompletion ─────────────────────────────────────────────────────────────
resource "aws_lambda_function" "jobcompletion" {
  function_name    = "${var.name_prefix}-jobcompletion"
  description      = "Correlates terminal Batch Operations job events to a run and sends the single deferred completion notification."
  role             = aws_iam_role.jobcompletion.arn
  runtime          = var.lambda_runtime
  handler          = "index.handler"
  timeout          = 60
  memory_size      = 256
  filename         = data.archive_file.jobcompletion.output_path
  source_code_hash = data.archive_file.jobcompletion.output_base64sha256
  tags             = local.tags

  dead_letter_config {
    target_arn = aws_sqs_queue.pipeline_dlq.arn
  }

  environment {
    variables = {
      SOLUTION_BUCKET = aws_s3_bucket.solution.id
      SNS_TOPIC_ARN   = aws_sns_topic.this.arn
      STACK_NAME      = var.name_prefix
    }
  }

  depends_on = [
    aws_iam_role_policy.jobcompletion,
    aws_cloudwatch_log_group.jobcompletion,
  ]
}

# ── Invoke permissions ────────────────────────────────────────────────────────
# S3 -> the three event-driven pipeline functions.
resource "aws_lambda_permission" "startquery_s3" {
  statement_id   = "AllowS3Invoke"
  action         = "lambda:InvokeFunction"
  function_name  = aws_lambda_function.startquery.function_name
  principal      = "s3.amazonaws.com"
  source_account = local.account_id
  source_arn     = aws_s3_bucket.solution.arn
}

resource "aws_lambda_permission" "manifestmaker_s3" {
  statement_id   = "AllowS3Invoke"
  action         = "lambda:InvokeFunction"
  function_name  = aws_lambda_function.manifestmaker.function_name
  principal      = "s3.amazonaws.com"
  source_account = local.account_id
  source_arn     = aws_s3_bucket.solution.arn
}

resource "aws_lambda_permission" "createjob_s3" {
  statement_id   = "AllowS3Invoke"
  action         = "lambda:InvokeFunction"
  function_name  = aws_lambda_function.createjob.function_name
  principal      = "s3.amazonaws.com"
  source_account = local.account_id
  source_arn     = aws_s3_bucket.solution.arn
}

# EventBridge -> JobCompletion.
resource "aws_lambda_permission" "jobcompletion_events" {
  statement_id  = "AllowEventBridgeInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.jobcompletion.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.job_completion.arn
}
