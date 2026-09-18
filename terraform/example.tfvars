# Example variable values. Copy to terraform.tfvars and edit, or pass with
# -var-file. See variables.tf for the full set and defaults.

# Required
target_bucket = "my-object-lock-bucket"
release_mode  = "delete" # delete | overwrite | either

# Optional (defaults shown)
name_prefix        = "auto-event-hold-release"
prefix             = ""
detection_schedule = "WEEKLY" # DAILY | WEEKLY
report_only        = true
safety_threshold   = 1000
kms_key_arn        = "" # arn:aws:kms:<region>:<account>:key/<id> to enable SSE-KMS

tags = {
  Solution = "auto-event-hold-release"
}
