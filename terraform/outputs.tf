output "lowercase_name" {
  description = "The name_prefix converted to lowercase; used to derive lowercase-safe resource names."
  value       = local.name
}

output "solution_bucket_name" {
  description = "Name of the solution-owned S3 bucket receiving inventory deliveries, Athena/UNLOAD output, combined manifests, and Batch Operations completion reports."
  value       = aws_s3_bucket.solution.id
}

output "sns_topic_arn" {
  description = "ARN of the SNS topic for run summaries and notifications."
  value       = aws_sns_topic.this.arn
}

output "startquery_function_name" {
  description = "Name of the inventory-triggered eligibility Lambda function."
  value       = aws_lambda_function.startquery.function_name
}

output "createjob_function_name" {
  description = "Name of the verified Batch Operations job-creation Lambda function."
  value       = aws_lambda_function.createjob.function_name
}

output "batch_operations_role_name" {
  description = "Name of the S3 Batch Operations service role used by release jobs."
  value       = aws_iam_role.batchops.name
}

output "batch_operations_role_arn" {
  description = "ARN of the S3 Batch Operations service role used by release jobs."
  value       = aws_iam_role.batchops.arn
}

output "glue_database_name" {
  description = "Name of the Glue database holding the inventory and dedup tables."
  value       = aws_glue_catalog_database.this.name
}

output "athena_workgroup_name" {
  description = "Name of the Athena workgroup used for eligibility queries."
  value       = aws_athena_workgroup.this.name
}
