# ─────────────────────────────────────────────────────────────────────────────
# CloudWatch monitoring: two alarms. One on the pipeline path where a run can
# complete, release holds, and notify nobody; one on the dead-letter queue where
# a run that failed outright lands.
# ─────────────────────────────────────────────────────────────────────────────

# Metric filter: a job completion event arrived for a run whose _active_jobs.json
# record was missing (holds released, but no completion email / _job_summary.json).
resource "aws_cloudwatch_log_metric_filter" "jobcompletion_missing_record" {
  name           = "${var.name_prefix}-jobcompletion-missing-record"
  log_group_name = aws_cloudwatch_log_group.jobcompletion.name
  # Substring match (survives both bare-JSON and JSON-wrapped log formats).
  pattern = "\"jobcompletion_no_active_jobs_record\""

  metric_transformation {
    name      = "JobCompletionMissingActiveJobsRecord"
    namespace = "${var.name_prefix}/EventHoldRelease"
    value     = "1"
    unit      = "Count"
    # No default_value on purpose — publish a data point only on a match so a
    # quiet month incurs no custom-metric charge.
  }
}

resource "aws_cloudwatch_metric_alarm" "jobcompletion_missing_record" {
  alarm_name        = "${var.name_prefix}-jobcompletion-missing-record"
  alarm_description = "A Batch Operations job completion event arrived for a run whose _active_jobs.json record was missing, so event holds were released without a completion notification and without a _job_summary.json."

  namespace           = "${var.name_prefix}/EventHoldRelease"
  metric_name         = "JobCompletionMissingActiveJobsRecord"
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.this.arn]
  tags                = local.tags
}

# Metric filter above publishes to a custom namespace; the alarm depends on it.
resource "aws_cloudwatch_metric_alarm" "pipeline_dlq_not_empty" {
  alarm_name        = "${var.name_prefix}-pipeline-dlq-not-empty"
  alarm_description = "A pipeline Lambda (StartQuery, ManifestMaker, CreateJob or JobCompletion) failed every retry and its invocation was dead-lettered, so this run produced no eligibility results and no run summary. Nothing was released."

  namespace   = "AWS/SQS"
  metric_name = "ApproximateNumberOfMessagesVisible"
  dimensions = {
    QueueName = aws_sqs_queue.pipeline_dlq.name
  }
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.this.arn]
  ok_actions          = [aws_sns_topic.this.arn]
  tags                = local.tags
}
