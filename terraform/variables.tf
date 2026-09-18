# ─────────────────────────────────────────────────────────────────────────────
# Input variables
#
# These map 1:1 to the CloudFormation template's Parameters, minus the optional
# server-access-logs / S3-Tables parameters which are intentionally not ported
# (see README.md "What was dropped"). `name_prefix` replaces the implicit
# ${AWS::StackName} that every resource name in the template was derived from.
# ─────────────────────────────────────────────────────────────────────────────

variable "name_prefix" {
  description = <<-EOT
    Name prefix for every resource this module creates. Replaces the
    CloudFormation stack name that the template used to derive resource
    names. Keep it short and DNS-safe: it is lowercased and truncated to 32
    characters to build the solution bucket name, and used verbatim for role,
    function, queue, topic and log-group names.
  EOT
  type        = string
  default     = "auto-event-hold-release"

  validation {
    condition     = can(regex("^[A-Za-z0-9][A-Za-z0-9-]{1,61}[A-Za-z0-9]$", var.name_prefix))
    error_message = "name_prefix must be 3-63 chars, alphanumeric and hyphens, starting and ending alphanumeric."
  }
}

variable "target_bucket" {
  description = <<-EOT
    Name of the S3 bucket (with Object Lock and Versioning enabled) whose
    noncurrent object versions this solution evaluates for event-hold release.
  EOT
  type        = string

  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$", var.target_bucket))
    error_message = "Must be a valid S3 bucket name: 3-63 chars, lowercase letters, numbers and hyphens, starting and ending with a letter or number."
  }
}

variable "prefix" {
  description = <<-EOT
    Optional S3 object key prefix. When set, only versions whose keys begin with
    this prefix are evaluated. Leave blank to evaluate the entire bucket.
  EOT
  type        = string
  default     = ""
}

variable "release_mode" {
  description = <<-EOT
    Which class of superseded noncurrent versions is eligible for event-hold
    release. "delete" acts only on versions superseded by a delete marker;
    "overwrite" only on versions superseded by a data version; "either" both.
  EOT
  type        = string

  validation {
    condition     = contains(["delete", "overwrite", "either"], var.release_mode)
    error_message = "Must be one of delete, overwrite, or either."
  }
}

variable "detection_schedule" {
  description = <<-EOT
    S3 Inventory generation cadence. DAILY delivers inventory once per day;
    WEEKLY once per week. Governs how often the solution evaluates the target
    bucket.
  EOT
  type        = string
  default     = "WEEKLY"

  validation {
    condition     = contains(["DAILY", "WEEKLY"], var.detection_schedule)
    error_message = "Must be DAILY or WEEKLY."
  }
}

variable "report_only" {
  description = <<-EOT
    When true, the solution identifies eligible versions and produces
    eligibility manifests but does not release any holds (Batch Operations jobs
    are created suspended for inspection). When false, it actively releases
    holds each run.
  EOT
  type        = bool
  default     = true
}

variable "safety_threshold" {
  description = <<-EOT
    Maximum number of event-hold releases an active run may submit to S3 Batch
    Operations. If a mode's verified eligible count exceeds this value, no job
    is created and the withheld mode is recorded for review. Has no effect when
    report_only is true.
  EOT
  type        = number
  default     = 1000

  validation {
    condition     = var.safety_threshold >= 0
    error_message = "safety_threshold must be >= 0."
  }
}

variable "kms_key_arn" {
  description = <<-EOT
    Optional ARN of a customer managed KMS key. When provided, it encrypts the
    solution bucket (SSE-KMS), the SNS topic, the pipeline dead-letter queue,
    and every Lambda log group. When blank, the bucket uses SSE-S3, SNS uses its
    default AWS-owned key, and log groups are unencrypted. The key policy must
    grant the four Lambda execution roles and the Batch Operations role
    kms:GenerateDataKey*/kms:Decrypt (see PERMISSIONS.md).
  EOT
  type        = string
  default     = ""

  validation {
    condition     = var.kms_key_arn == "" || can(regex("^arn:(aws|aws-us-gov|aws-cn):kms:[a-z0-9-]+:[0-9]{12}:key/[a-f0-9-]+$", var.kms_key_arn))
    error_message = "Must be a valid KMS key ARN or blank."
  }
}

variable "lambda_runtime" {
  description = "Python runtime for the pipeline Lambda functions."
  type        = string
  default     = "python3.14"
}

variable "boto3_layer_version" {
  description = <<-EOT
    boto3 version bundled into a Lambda layer and attached to the CreateJob
    function. The managed runtime's bundled botocore does not declare the
    EventHold parameter on the s3control retention shape, so create_job is
    rejected inside the function and GOVERNANCE releases are withheld. Layering
    this boto3 ahead of the runtime SDK restores the parameter. Must be a
    version whose botocore carries EventHold (botocore >= 1.43.0).
  EOT
  type        = string
  default     = "1.43.98"

  validation {
    condition     = can(regex("^[0-9]+\\.[0-9]+\\.[0-9]+$", var.boto3_layer_version))
    error_message = "Must be an exact boto3 version, e.g. 1.43.98."
  }
}

variable "log_retention_days" {
  description = "Retention (days) for the Lambda CloudWatch log groups."
  type        = number
  default     = 90
}

variable "tags" {
  description = "Tags applied to all taggable resources."
  type        = map(string)
  default     = {}
}
