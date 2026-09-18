locals {
  account_id = data.aws_caller_identity.current.account_id
  region     = data.aws_region.current.region
  partition  = data.aws_partition.current.partition

  # Replaces the LowerCase custom resource. `name` is the lowercased name_prefix
  # (Glue requires lowercase database names); `bucket_prefix` truncates it to 40
  # chars so the account-id + region suffix keeps the bucket name within S3's
  # 63-char limit.
  name          = lower(var.name_prefix)
  bucket_prefix = substr(lower(var.name_prefix), 0, 40)

  # SolutionBucket name. The CloudFormation template used
  # BucketNamespace=account-regional, which produces an AWS-managed
  # "account-regional namespace" bucket whose name ends in "-an". Terraform's
  # aws_s3_bucket resource cannot create namespace buckets — a plain CreateBucket
  # with a name ending in "-an" is rejected by both the provider and the S3 API
  # ("... is an account-regional namespace bucket"). This solution bucket is just
  # an internal working bucket (inventory / manifests / reports), so it does not
  # need to be a namespace bucket. We keep the account-id + region suffix so the
  # name stays predictable and globally unique, and drop the reserved "-an".
  solution_bucket_name = "${local.bucket_prefix}-${local.account_id}-${local.region}"

  glue_database_name = "${local.name}-db"
  athena_workgroup   = "${local.name}-wg"

  has_kms_key = var.kms_key_arn != ""

  # Fixed logical names used by the pipeline code and Glue.
  glue_table_name              = "inventory"
  previous_manifest_table_name = "previous_manifest"
  withheld_history_table_name  = "withheld_candidates_history"

  # Object wildcard scoping for the Batch Operations release grant:
  # arn:...:${target_bucket}/${prefix}* (matches the template's PW substitution).
  target_object_arn = "arn:${local.partition}:s3:::${var.target_bucket}/${var.prefix}*"

  tags = var.tags
}
