import json
import os
import re
import boto3
from botocore.exceptions import ClientError

_TERMINAL_STATUSES = frozenset(['Complete', 'Cancelled', 'Failed'])
_JOB_TAG_KEY = 'job-created-by'

# Bound on the conditional read-modify-write of _active_jobs.json below.
# A run creates at most one job per ObjectLockMode, so at most two
# writers ever contend for this key and one retry would do; four leaves
# room for Lambda's own async redrives arriving in the same window
# without ever looping for long. Exhausting it raises, which lands in
# PipelineDLQ and is alarmed.
_RECORD_WRITE_ATTEMPTS = 4

# PutObject with IfMatch fails with PreconditionFailed when the ETag has
# moved, and with ConditionalRequestConflict when another conditional
# write to the same key is in flight. Both mean "someone else got there
# first, re-read and re-apply", so both are retried rather than raised.
_CONDITIONAL_WRITE_LOST = frozenset(
    ['PreconditionFailed', 'ConditionalRequestConflict']
)

# run_id is the S3 event sequencer StartQuery validated with the same
# pattern before using it in a key path. Here it arrives indirectly, as a
# BOPS job tag read back via GetJobTagging, and is used to build
# manifests/combined/{run_id}/ keys -- so it is re-validated rather than
# trusted as pre-validated. Tags are writable by anything holding
# s3:PutJobTagging on a job carrying this stack's job-created-by value.
_RUN_ID_PATTERN = re.compile(r'\A[0-9A-Fa-f]{1,64}\Z')


def _build_completion_summary(record):
    """Plain-English recap of a completed run, mirroring
    CreateJobFunction's _build_human_summary style but reporting actual
    per-job outcomes instead of dispatch intent.
    """
    lines = [
        f"Run {record['run_id']} against {record['target_bucket']} "
        f"(inventory {record['dt']}) -- active mode, all jobs finished."
    ]
    for mode, job in sorted(record['jobs'].items()):
        status = job['status']
        succeeded = job.get('succeeded', 0)
        failed = job.get('failed', 0)
        total = job.get('total', succeeded + failed)
        if status == 'Complete' and failed == 0:
            lines.append(
                f"{mode}: released {succeeded} of {total} event hold"
                f"{'s' if total != 1 else ''} (job {job['job_id']})."
            )
        elif status == 'Complete':
            lines.append(
                f"{mode}: released {succeeded} of {total} event holds, "
                f"{failed} failed -- see reports/{record['run_id']}/"
                f"{mode}/ for details (job {job['job_id']})."
            )
        else:
            reason = job.get('failure_reason', 'see job details')
            lines.append(
                f"{mode}: job {job['job_id']} ended {status} -- {reason}."
            )
    for mode, job_id in sorted(record.get('suspended_jobs', {}).items()):
        count = record.get('verified_counts', {}).get(mode, 0)
        lines.append(
            f"{mode}: found {count} eligible version"
            f"{'s' if count != 1 else ''}, job {job_id} created "
            "suspended for review (report-only) -- nothing released."
        )
    for mode in sorted(record.get('modes_skipped', [])):
        lines.append(f"{mode}: no eligible versions this run.")
    for mode, detail in sorted(record.get('withheld_modes', {}).items()):
        lines.append(
            f"{mode}: WITHHELD -- {detail.get('reason', 'unknown')}."
        )
    return ' '.join(lines)


# See CreateJobFunction's copy of these for why the bucket name is elided
# rather than truncated by the caller's [:100] slice.
_SUBJECT_MAX = 100
_SUBJECT_STEM = 'Auto Remove Event Hold: '
_SUBJECT_MIN_BUCKET = 20


def _fit_subject(outcome, bucket, tag=''):
    """Assemble '{stem}{outcome}{tag} - {bucket}' inside the 100-character
    limit. The bucket name's middle is elided if the line would overflow;
    only if fewer than _SUBJECT_MIN_BUCKET characters of bucket would
    survive does the outcome drop whole trailing clauses (never half a
    printed number). Everything appears untruncated in the message body.
    """
    room = _SUBJECT_MAX - len(_SUBJECT_STEM) - len(tag) - len(' - ')
    reserved = min(len(bucket), _SUBJECT_MIN_BUCKET)
    if len(outcome) > room - reserved:
        clauses = outcome.split(', ')
        kept = [clauses[0]]
        while len(kept) < len(clauses) and (
            len(', '.join(kept + [clauses[len(kept)]])) + 3
            <= room - reserved
        ):
            kept.append(clauses[len(kept)])
        outcome = ', '.join(kept)
        if len(kept) < len(clauses):
            outcome += '...'
        outcome = outcome[:room - reserved]

    head = f'{_SUBJECT_STEM}{outcome}{tag} - '
    budget = _SUBJECT_MAX - len(head)
    if len(bucket) > budget:
        keep = max(budget - 3, 2)
        bucket = (
            f'{bucket[:(keep + 1) // 2]}...{bucket[-(keep // 2):]}'
        )
    return f'{head}{bucket}'


def _build_completion_subject(record):
    """SNS Subject line for a finished run. Version counts lead because
    that is the work actually done; job counts appear only where they carry
    information the version counts do not (a job that never completed
    released nothing and reported no per-object numbers). Bucket last,
    run_id in the body -- same reasoning as CreateJobFunction's
    _build_subject.
    """
    jobs = list(record['jobs'].values())
    complete = [j for j in jobs if j['status'] == 'Complete']
    incomplete = [
        j for j in jobs if j['status'] in ('Cancelled', 'Failed')
    ]
    released = sum(j.get('succeeded', 0) for j in complete)
    failed = sum(j.get('failed', 0) for j in complete)
    total = released + failed

    parts = []
    if total:
        if failed:
            parts.append(
                f'{released:,} of {total:,} '
                f"version{'s' if total != 1 else ''} released, "
                f'{failed:,} failed'
            )
        else:
            parts.append(
                f'{released:,} '
                f"version{'s' if released != 1 else ''} released"
            )
    if incomplete:
        count = len(incomplete)
        parts.append(
            f"{count} job{'s' if count != 1 else ''} did not complete"
        )
    outcome = ', '.join(parts) if parts else 'finished, nothing released'

    return _fit_subject(outcome, record['target_bucket'])


def handler(event, context):
    solution_bucket = os.environ['SOLUTION_BUCKET']
    sns_topic_arn = os.environ['SNS_TOPIC_ARN']
    stack_name = os.environ['STACK_NAME']
    expected_job_tag_value = f'Auto Remove Event Hold Solution:{stack_name}'

    detail = event.get('detail', {})
    service_details = detail.get('serviceEventDetails', {})
    job_id = service_details.get('jobId')
    status = service_details.get('status')

    if not job_id or status not in _TERMINAL_STATUSES:
        # Intermediate state (New/Preparing/Active/etc) or malformed
        # event -- nothing to do. The EventBridge rule itself already
        # filters to terminal statuses; this is defense in depth against
        # a manually-sent or malformed test event.
        print(json.dumps({
            'event': 'jobcompletion_ignored_non_terminal',
            'job_id': job_id, 'status': status,
        }))
        return {'ignored': True}

    s3control = boto3.client('s3control')
    account_id = context.invoked_function_arn.split(':')[4]

    try:
        job = s3control.describe_job(
            AccountId=account_id, JobId=job_id,
        )['Job']
    except ClientError as exc:
        print(json.dumps({
            'event': 'jobcompletion_describe_job_failed',
            'job_id': job_id, 'detail': str(exc)[:500],
        }))
        raise

    # The event's status is a filter; DescribeJob's is the fact. What
    # reaches the durable run record comes from the API, because the
    # payload is attacker-supplied on any direct invocation and this
    # record is the audit trail for removing WORM protection. The gate
    # above still uses the event so an intermediate-state event costs
    # nothing.
    #
    # A forged terminal event for a still-Active job used to be recorded
    # as terminal with that job's partial ProgressSummary, consuming the
    # run's one completion claim and understating what was released.
    # ProgressSummary and FailureReasons already came from this response,
    # so Status was the last field taken on trust.
    event_status = status
    status = job.get('Status')
    if status != event_status:
        print(json.dumps({
            'event': 'jobcompletion_status_disagreement',
            'job_id': job_id,
            'event_status': event_status,
            'describe_job_status': status,
        }))
    if status not in _TERMINAL_STATUSES:
        print(json.dumps({
            'event': 'jobcompletion_job_not_terminal',
            'job_id': job_id, 'describe_job_status': status,
        }))
        return {'ignored': True, 'reason': 'job_not_terminal'}

    # DescribeJob does NOT return a job's tags (confirmed: 'Tags' is
    # absent from its response entirely) -- tags are a separate
    # GetJobTagging API. Without this, every real BOPS-dispatched job
    # completion was misclassified as jobcompletion_ignored_foreign_job
    # and silently dropped, confirmed against a real deployed stack.
    try:
        tagging = s3control.get_job_tagging(
            AccountId=account_id, JobId=job_id,
        )
    except ClientError as exc:
        print(json.dumps({
            'event': 'jobcompletion_get_job_tagging_failed',
            'job_id': job_id, 'detail': str(exc)[:500],
        }))
        raise

    tags = {t['Key']: t['Value'] for t in tagging.get('Tags', [])}
    if tags.get(_JOB_TAG_KEY) != expected_job_tag_value:
        # Not one of this stack's jobs (or not one of this solution's
        # jobs at all) -- the account/Region may have other, unrelated
        # Batch Operations jobs; the EventBridge rule matches on status
        # only, not on tags (create_job's tags aren't queryable in an
        # EventBridge event pattern), so this Lambda is the actual
        # filter. Ignore, not an error.
        print(json.dumps({
            'event': 'jobcompletion_ignored_foreign_job', 'job_id': job_id,
        }))
        return {'ignored': True, 'reason': 'not_this_solution'}

    run_id = tags.get('run-id')
    if not run_id:
        print(json.dumps({
            'event': 'jobcompletion_missing_run_id_tag', 'job_id': job_id,
        }))
        return {'ignored': True, 'reason': 'missing_run_id_tag'}
    if not _RUN_ID_PATTERN.match(run_id):
        # Fail closed before any key is built from it, and ignore rather
        # than raise: a legitimate run_id is always a hex sequencer that
        # matches, so a value that does not is either a malformed or a
        # tampered tag, neither of which a DLQ retry can resolve.
        print(json.dumps({
            'event': 'jobcompletion_invalid_run_id_tag',
            'job_id': job_id, 'run_id': run_id[:64],
        }))
        return {'ignored': True, 'reason': 'invalid_run_id_tag'}

    record_key = f'manifests/combined/{run_id}/_active_jobs.json'
    s3 = boto3.client('s3')

    # _active_jobs.json holds one entry per job the run created, and
    # one invocation per job flips its own entry, so this is a
    # read-modify-write under concurrency: conditional on the ETag it
    # read, retried when it loses.
    #
    # An unconditional write is safe for repeated deliveries of the same
    # event and unsafe for two jobs' events racing, which is the normal
    # case whenever both lock modes have eligible versions. Each
    # invocation read both jobs pending, flipped its own, and the second
    # write restored the first to pending. No further event existed for
    # it, so the run never notified and never wrote a summary while both
    # jobs had already released holds.
    #
    # Retrying also puts the right invocation on the completion path:
    # the loser re-reads, sees the other job terminal, and is the one
    # that observes all-terminal against an accurate record.
    progress = job.get('ProgressSummary', {})
    reasons = job.get('FailureReasons', [])
    record = None
    still_pending = True

    for attempt in range(_RECORD_WRITE_ATTEMPTS):
        try:
            obj = s3.get_object(Bucket=solution_bucket, Key=record_key)
        except ClientError as exc:
            if exc.response['Error']['Code'] in ('NoSuchKey', '404'):
                # Either already fully processed (record replaced by the
                # final _job_summary.json write below) or this event
                # somehow arrived for a run this stack never tracked --
                # either way, nothing to do.
                print(json.dumps({
                    'event': 'jobcompletion_no_active_jobs_record',
                    'run_id': run_id, 'job_id': job_id,
                }))
                return {
                    'ignored': True,
                    'reason': 'no_active_jobs_record',
                }
            raise
        record = json.loads(obj['Body'].read())
        record_etag = obj['ETag']

        mode = None
        for candidate_mode, job_info in record['jobs'].items():
            if job_info['job_id'] == job_id:
                mode = candidate_mode
                break
        if mode is None:
            print(json.dumps({
                'event': 'jobcompletion_job_not_in_record',
                'run_id': run_id, 'job_id': job_id,
            }))
            return {'ignored': True, 'reason': 'job_not_in_record'}

        if record['jobs'][mode]['status'] != 'pending':
            # Already recorded as terminal by an earlier delivery of
            # this same event (EventBridge/CloudTrail delivery is
            # at-least-once, same class of duplicate-delivery problem
            # CreateJobFunction's _createjob_lock.json already guards
            # against). No-op. Re-checked on every attempt, because a
            # lost conditional write can also mean a duplicate of this
            # very event won the race.
            print(json.dumps({
                'event': 'jobcompletion_duplicate_event_skipped',
                'run_id': run_id, 'job_id': job_id,
            }))
            return {'ignored': True, 'reason': 'duplicate_event'}

        record['jobs'][mode].update({
            'status': status,
            'succeeded': progress.get('NumberOfTasksSucceeded', 0),
            'failed': progress.get('NumberOfTasksFailed', 0),
            'total': progress.get('TotalNumberOfTasks', 0),
        })
        if status != 'Complete':
            record['jobs'][mode]['failure_reason'] = (
                reasons[0].get('FailureReason', status)
                if reasons else status
            )

        still_pending = any(
            j['status'] == 'pending' for j in record['jobs'].values()
        )

        # Written on both paths, so the object always reflects what this
        # invocation decided, including the final all-terminal state.
        try:
            s3.put_object(
                Bucket=solution_bucket, Key=record_key,
                Body=json.dumps(record),
                ContentType='application/json',
                IfMatch=record_etag,
            )
        except ClientError as exc:
            if exc.response['Error']['Code'] in _CONDITIONAL_WRITE_LOST:
                print(json.dumps({
                    'event': 'jobcompletion_record_write_retry',
                    'run_id': run_id, 'job_id': job_id,
                    'attempt': attempt + 1,
                }))
                continue
            raise
        break
    else:
        # Raise rather than continue: carrying on would mean notifying
        # from a record this invocation could not persist. This lands in
        # PipelineDLQ, which is alarmed.
        raise RuntimeError(
            f"Could not update {record_key} within "
            f"{_RECORD_WRITE_ATTEMPTS} attempts for job {job_id}"
        )

    if still_pending:
        # Wait for the remaining job(s) this run created; the invocation
        # handling the last of them sends the notification.
        print(json.dumps({
            'event': 'jobcompletion_partial', 'run_id': run_id,
            'job_id': job_id, 'status': status,
        }))
        return {'run_id': run_id, 'job_id': job_id, 'partial': True}

    # Every active job this run created is now terminal. Claim the
    # completion send via a conditional PutObject (IfNoneMatch='*'),
    # the same pattern CreateJobFunction's _createjob_lock.json already
    # uses for the identical class of problem: multiple invocations can
    # independently observe "all terminal" for the same run (Lambda's
    # own async retries after a transient failure, EventBridge/
    # CloudTrail's at-least-once delivery, or simply two different
    # jobs' JobStatusChanged events both landing after the run's last
    # job actually finishes) and would otherwise all reach this point
    # and each send a duplicate email. Confirmed against a real
    # deployed stack: an earlier AccessDenied-then-retried invocation
    # and a separate manual re-invocation both got here and both sent
    # an email before this lock was added.
    try:
        s3.put_object(
            Bucket=solution_bucket,
            Key=f'manifests/combined/{run_id}/_completion_sent.json',
            Body=json.dumps({
                'run_id': run_id, 'request_id': context.aws_request_id,
            }),
            ContentType='application/json',
            IfNoneMatch='*',
        )
    except ClientError as exc:
        if exc.response['Error']['Code'] == 'PreconditionFailed':
            print(json.dumps({
                'event': 'jobcompletion_duplicate_send_skipped',
                'run_id': run_id,
            }))
            return {'run_id': run_id, 'skipped': 'duplicate_send'}
        raise

    summary = {
        'summary': _build_completion_summary(record),
        'run_id': run_id,
        'decision_id': record['decision_id'],
        'dt': record['dt'],
        'target_bucket': record['target_bucket'],
        'report_only': False,
        'jobs': record['jobs'],
        'eligible_counts': record.get('verified_counts', {}),
        'jobs_suspended': record.get('suspended_jobs', {}),
        'modes_withheld': record.get('withheld_modes', {}),
        'modes_skipped': record.get('modes_skipped', []),
        'safety_threshold': record.get('safety_threshold'),
        'withheld_candidate_count': record.get(
            'withheld_candidate_count', 0),
    }
    subject = _build_completion_subject(record)
    sns = boto3.client('sns')
    sns.publish(
        TopicArn=sns_topic_arn,
        Subject=subject[:100],
        Message=json.dumps(summary, indent=2),
    )
    summary_body = json.dumps(summary)
    s3.put_object(
        Bucket=solution_bucket,
        Key=(
            f'manifests/combined/{run_id}/_job_summaries/'
            f'{context.aws_request_id}.json'
        ),
        Body=summary_body,
        ContentType='application/json',
    )
    s3.put_object(
        Bucket=solution_bucket,
        Key=f'manifests/combined/{run_id}/_job_summary.json',
        Body=summary_body,
        ContentType='application/json',
    )
    print(json.dumps({
        'event': 'jobcompletion_run_complete', 'run_id': run_id,
    }))
    return summary
