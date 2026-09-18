# ─────────────────────────────────────────────────────────────────────────────
# SolutionBucket — receives inventory deliveries, Athena/UNLOAD output, combined
# manifests, and Batch Operations completion reports. Kept separate from the
# target bucket's protected data. Retained on destroy, matching the template's
# DeletionPolicy: Retain (Terraform: prevent_destroy).
# ─────────────────────────────────────────────────────────────────────────────

resource "aws_s3_bucket" "solution" {
  bucket = local.solution_bucket_name
  tags   = local.tags

  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_s3_bucket_public_access_block" "solution" {
  bucket                  = aws_s3_bucket.solution.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "solution" {
  bucket = aws_s3_bucket.solution.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = local.has_kms_key ? "aws:kms" : "AES256"
      kms_master_key_id = local.has_kms_key ? var.kms_key_arn : null
    }
    bucket_key_enabled = local.has_kms_key ? true : null
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "solution" {
  bucket = aws_s3_bucket.solution.id

  # 14-day working-artifact prefixes.
  rule {
    id     = "ExpireInventorySnapshots"
    status = "Enabled"
    filter { prefix = "inventory/" }
    expiration { days = 14 }
  }

  rule {
    id     = "ExpireManifestParts"
    status = "Enabled"
    filter { prefix = "manifests/parts/" }
    expiration { days = 14 }
  }

  # 90 days: combined manifests also hold the run's decision records
  # (_job_summary.json / _job_summaries/*), so they match the completion-report
  # window rather than the 14-day working prefixes.
  rule {
    id     = "ExpireCombinedManifests"
    status = "Enabled"
    filter { prefix = "manifests/combined/" }
    expiration { days = 90 }
  }

  rule {
    id     = "ExpireAthenaResults"
    status = "Enabled"
    filter { prefix = "athena-results/" }
    expiration { days = 14 }
  }

  rule {
    id     = "ExpireCompletionReports"
    status = "Enabled"
    filter { prefix = "reports/" }
    expiration { days = 90 }
  }

  rule {
    id     = "ExpireWithheldCandidatesDiagnostics"
    status = "Enabled"
    filter { prefix = "diagnostics/withheld-candidates/" }
    expiration { days = 90 }
  }

  # No expiry on manifests/previous/ — it holds at most one small CSV per mode,
  # a live dedup pointer, not an accumulating artifact.

  rule {
    id     = "AbortIncompleteMultipartUploads"
    status = "Enabled"
    filter {}
    abort_incomplete_multipart_upload { days_after_initiation = 3 }
  }
}

# ── SolutionBucket policy ─────────────────────────────────────────────────────
# Mirrors SolutionBucketPolicy: allow S3 Inventory delivery, deny unauthorized
# writes to working prefixes (all principals except the pipeline roles + the S3
# service), protect completion-report immutability, enforce TLS, and (when a CMK
# is supplied) pin SSE-KMS to this stack's key.

data "aws_iam_policy_document" "solution_bucket" {
  statement {
    sid     = "AllowS3InventoryDelivery"
    effect  = "Allow"
    actions = ["s3:PutObject"]
    principals {
      type        = "Service"
      identifiers = ["s3.amazonaws.com"]
    }
    resources = ["${aws_s3_bucket.solution.arn}/inventory/*"]
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }
    condition {
      test     = "ArnLike"
      variable = "aws:SourceArn"
      values   = ["arn:${local.partition}:s3:::${var.target_bucket}"]
    }
  }

  statement {
    sid     = "DenyUnauthorizedPutObject"
    effect  = "Deny"
    actions = ["s3:PutObject"]
    resources = [
      "${aws_s3_bucket.solution.arn}/inventory/*",
      "${aws_s3_bucket.solution.arn}/manifests/*",
      "${aws_s3_bucket.solution.arn}/athena-results/*",
      "${aws_s3_bucket.solution.arn}/diagnostics/withheld-candidates/*",
    ]
    principals {
      type        = "AWS"
      identifiers = ["*"]
    }
    condition {
      test     = "ArnNotLike"
      variable = "aws:PrincipalArn"
      values = [
        aws_iam_role.startquery.arn,
        aws_iam_role.manifestmaker.arn,
        aws_iam_role.createjob.arn,
        aws_iam_role.jobcompletion.arn,
      ]
    }
    condition {
      test     = "StringNotEqualsIfExists"
      variable = "aws:PrincipalServiceName"
      values   = ["s3.amazonaws.com"]
    }
  }

  statement {
    sid       = "DenyUnauthorizedReportsPut"
    effect    = "Deny"
    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.solution.arn}/reports/*"]
    principals {
      type        = "AWS"
      identifiers = ["*"]
    }
    condition {
      test     = "ArnNotLike"
      variable = "aws:PrincipalArn"
      values   = [aws_iam_role.batchops.arn]
    }
  }

  statement {
    sid       = "DenyUnencryptedTransport"
    effect    = "Deny"
    actions   = ["s3:*"]
    resources = [aws_s3_bucket.solution.arn, "${aws_s3_bucket.solution.arn}/*"]
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

  # KMS-key pinning statements only when a CMK is supplied.
  dynamic "statement" {
    for_each = local.has_kms_key ? [1] : []
    content {
      sid       = "DenyMismatchedEncryption"
      effect    = "Deny"
      actions   = ["s3:PutObject"]
      resources = ["${aws_s3_bucket.solution.arn}/*"]
      principals {
        type        = "AWS"
        identifiers = ["*"]
      }
      condition {
        test     = "Null"
        variable = "s3:x-amz-server-side-encryption"
        values   = ["false"]
      }
      condition {
        test     = "StringNotEquals"
        variable = "s3:x-amz-server-side-encryption"
        values   = ["aws:kms"]
      }
    }
  }

  dynamic "statement" {
    for_each = local.has_kms_key ? [1] : []
    content {
      sid       = "DenyMismatchedKmsKey"
      effect    = "Deny"
      actions   = ["s3:PutObject"]
      resources = ["${aws_s3_bucket.solution.arn}/*"]
      principals {
        type        = "AWS"
        identifiers = ["*"]
      }
      condition {
        test     = "StringEquals"
        variable = "s3:x-amz-server-side-encryption"
        values   = ["aws:kms"]
      }
      condition {
        test     = "StringNotEqualsIfExists"
        variable = "s3:x-amz-server-side-encryption-aws-kms-key-id"
        values   = [var.kms_key_arn]
      }
    }
  }
}

resource "aws_s3_bucket_policy" "solution" {
  bucket = aws_s3_bucket.solution.id
  policy = data.aws_iam_policy_document.solution_bucket.json
}
