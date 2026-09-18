import hashlib
import json
import os
import re
import urllib.parse
import boto3
import botocore.session
from botocore.exceptions import ClientError, ParamValidationError

# S3 Inventory delivery timestamp: YYYY-MM-DDThh-mmZ. Re-validated here
# even though StartQuery already checks it, because dt reaches CreateJob
# via a trigger file that passed through ManifestMaker rather than
# directly from the S3 event — it must not be trusted as pre-validated.
_DT_PATTERN = re.compile(
    r'\A[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}-[0-9]{2}Z\Z'
)
_ALLOWED_MODES = frozenset(['COMPLIANCE', 'GOVERNANCE'])

# Same hex pattern as StartQuery's _SEQUENCER_PATTERN and
# JobCompletion's _RUN_ID_PATTERN. Without this, CreateJob would
# accept a non-hex run_id that JobCompletion later rejects,
# creating an audit-evasion path where holds are released without
# a completion notification.
_RUN_ID_PATTERN = re.compile(r'\A[0-9A-Fa-f]{1,64}\Z')

# The AWS SDK validates call parameters locally, against its own bundled
# copy of the s3control service model, before it signs the request. A
# bundled model that does not declare EventHold on the retention shape
# therefore rejects this solution's create_job call inside the function,
# and the request never reaches S3. Lambda's managed runtimes supply their
# own boto3/botocore, and AWS documents that bundled version as varying by
# runtime version and Region rather than publishing it, so whether a given
# deployment can make this call is a property of the environment it lands
# in and not of this template. It is read at run time for that reason.
#
# A shape check rather than an SDK version comparison, deliberately. The
# version number is an indirect signal: interpreting it needs a lookup
# table of which release first carried the member, that table has to be
# revised whenever it changes, and a wrong entry either blocks a runtime
# that would have worked or lets a call through that cannot. Asking the
# loaded model whether it declares the member answers the same question
# directly and needs no maintenance.
_RETENTION_SHAPE = 'S3Retention'


def _event_hold_capability():
    """Report whether the bundled SDK declares EventHold on retention.

    Returns a dict, logged once per run and carried into the run summary.
    'event_hold_model' is 'native' when the loaded model declares the
    member, 'missing' when it does not, and 'unknown' when the check
    itself could not complete.
    """
    info = {
        'botocore_version': getattr(
            botocore, '__version__', 'unknown'
        ),
    }
    try:
        model = botocore.session.get_session().get_service_model(
            's3control'
        )
        members = model.shape_for(_RETENTION_SHAPE).members
        info['event_hold_model'] = (
            'native' if 'EventHold' in members else 'missing'
        )
        info['retention_members'] = sorted(members)
    except Exception as exc:
        # 'unknown' goes on to attempt create_job instead of withholding.
        # A check that failed is not evidence that the call would fail,
        # and the model-introspection methods used above are SDK
        # internals rather than a supported interface, so this must never
        # become the thing that stops a run which would have succeeded.
        # If the bundled model genuinely lacks the member, create_job
        # raises ParamValidationError and the handler records the same
        # withheld reason from there.
        info['event_hold_model'] = 'unknown'
        info['detail'] = str(exc)[:200]
    return info


def _verify_count_query(
    athena, query_execution_id, expected_hash, expected_workgroup,
):
    """Return COUNT(*) only for the expected successful Athena query."""
    response = athena.get_query_execution(
        QueryExecutionId=query_execution_id
    )
    execution = response['QueryExecution']
    state = execution['Status']['State']
    if state != 'SUCCEEDED':
        raise ValueError(
            f"Count query {query_execution_id} state is {state}, not SUCCEEDED"
        )
    if execution.get('WorkGroup') != expected_workgroup:
        raise ValueError(
            f"Count query {query_execution_id} ran in unexpected workgroup"
        )
    actual_hash = hashlib.sha256(
        execution.get('Query', '').encode()
    ).hexdigest()
    if actual_hash != expected_hash:
        raise ValueError(
            f"Count query {query_execution_id} SQL hash mismatch"
        )

    result = athena.get_query_results(
        QueryExecutionId=query_execution_id,
        MaxResults=2,
    )
    rows = result.get('ResultSet', {}).get('Rows', [])
    if len(rows) != 2:
        raise ValueError(
            f"Count query {query_execution_id} returned {len(rows)} rows"
        )
    data = rows[1].get('Data', [])
    raw_count = data[0].get('VarCharValue') if data else None
    if raw_count is None or not raw_count.isdigit():
        raise ValueError(
            f"Count query {query_execution_id} returned invalid count "
            f"{raw_count!r}"
        )
    return int(raw_count)


def _build_human_summary(
    run_id, dt, target_bucket, report_only, verified_counts,
    created_jobs, suspended_jobs, withheld_modes, skipped_modes,
):
    """Plain-English recap of this run, for the top of the SNS email /
    JSON summary -- everyone else reads the structured fields below it.
    """
    lines = [
        f"Run {run_id} against {target_bucket} (inventory {dt}), "
        f"{'report-only' if report_only else 'active'} mode."
    ]

    for mode, job_id in sorted(created_jobs.items()):
        count = verified_counts.get(mode, 0)
        lines.append(
            f"{mode}: released {count} event hold"
            f"{'s' if count != 1 else ''} (job {job_id})."
        )
    for mode, job_id in sorted(suspended_jobs.items()):
        count = verified_counts.get(mode, 0)
        lines.append(
            f"{mode}: found {count} eligible version"
            f"{'s' if count != 1 else ''}, job {job_id} created "
            "suspended for review (report-only) -- nothing released."
        )
    for mode in sorted(skipped_modes):
        lines.append(f"{mode}: no eligible versions this run.")
    for mode, detail in sorted(withheld_modes.items()):
        reason = detail.get('reason', 'unknown')
        if reason == 'safety_threshold_exceeded':
            lines.append(
                f"{mode}: WITHHELD -- {detail.get('eligible_count')} "
                f"eligible versions exceeds SafetyThreshold "
                f"({detail.get('safety_threshold')}); no job created, "
                "review and re-run to proceed."
            )
        elif reason == 'count_mismatch':
            lines.append(
                f"{mode}: WITHHELD -- eligibility count mismatch "
                f"(Athena {detail.get('athena_count')} vs manifest "
                f"{detail.get('trigger_count')}); no job created."
            )
        elif reason == 'event_hold_unsupported_by_runtime':
            lines.append(
                f"{mode}: WITHHELD -- the AWS SDK bundled in this "
                f"Lambda runtime (botocore "
                f"{detail.get('botocore_version', 'unknown')}) does "
                "not accept the EventHold parameter, so no Batch "
                "Operations job was created. Nothing was released and "
                "nothing was lost: these versions stay eligible, and "
                "the next run releases them once the runtime's bundled "
                "SDK carries the parameter. See OPERATIONS.md, "
                "\"Withheld: runtime SDK does not support EventHold\"."
            )
        elif reason == 'create_job_failed':
            lines.append(
                f"{mode}: WITHHELD -- Batch Operations job creation "
                f"failed ({detail.get('detail', 'see logs')})."
            )
        else:
            lines.append(f"{mode}: WITHHELD -- {reason}.")

    if len(lines) == 1:
        lines.append("No modes were evaluated for this run.")
    return ' '.join(lines)


# SNS Subject lines are ASCII-only and capped at 100 characters. The
# bucket name is the last field and the only variable-length free text in
# the line, so it is what overflows -- elide its middle rather than let
# the caller's [:100] slice amputate it, because both ends of a bucket
# name carry identifying information (environment prefix, Region suffix).
_SUBJECT_MAX = 100
_SUBJECT_STEM = 'Auto Remove Event Hold: '
# Room always reserved for the bucket name. Naming the bucket is the whole
# point of the line, and an elided-middle name still identifies a stack, so
# the outcome gives up trailing clauses before the bucket gives up
# characters.
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
        # Guards the case where even the first clause overruns the room.
        outcome = outcome[:room - reserved]

    head = f'{_SUBJECT_STEM}{outcome}{tag} - '
    budget = _SUBJECT_MAX - len(head)
    if len(bucket) > budget:
        keep = max(budget - 3, 2)
        bucket = (
            f'{bucket[:(keep + 1) // 2]}...{bucket[-(keep // 2):]}'
        )
    return f'{head}{bucket}'


def _build_subject(
    target_bucket, report_only, verified_counts, suspended_jobs,
    withheld_modes, skipped_modes,
):
    """SNS Subject line: lead with the outcome and the eligible version
    count so the mail is triageable from an inbox list without opening it,
    and name the Target_Bucket last so runs from different stacks are
    distinguishable at a glance. run_id is deliberately absent -- it is the
    first thing in the message body, and an opaque hex sequencer earns no
    space in a 100-character subject.

    This function is only reached when no ACTIVE job was created (a
    report-only run, a withheld run, or nothing eligible). Every real
    release is announced by JobCompletionFunction's
    _build_completion_subject instead, because the handler returns early
    and defers notification whenever it creates an active job.
    """
    counted = []
    if suspended_jobs:
        counted.append((len(suspended_jobs), 'pending'))
    if withheld_modes:
        counted.append((len(withheld_modes), 'withheld'))

    if counted:
        first_count, first_label = counted[0]
        parts = [
            f"{first_count} job{'s' if first_count != 1 else ''} "
            f"{first_label}"
        ]
        parts += [f'{count} {label}' for count, label in counted[1:]]
        # verified_counts holds the Athena-verified eligible version count
        # per mode. It is populated for every suspended mode and for modes
        # withheld on SafetyThreshold, but not for modes withheld before
        # verification completed (count_mismatch and friends) -- those
        # contribute 0 and the count is omitted entirely if none is known,
        # rather than asserting a number the run never established.
        versions = sum(
            verified_counts.get(mode, 0)
            for mode in list(suspended_jobs) + list(withheld_modes)
        )
        if versions:
            parts.append(
                f"{versions:,} version{'s' if versions != 1 else ''}"
            )
        outcome = ', '.join(parts)
    elif skipped_modes:
        outcome = 'nothing eligible'
    else:
        outcome = 'no modes evaluated'

    tag = ' [Report-Only]' if report_only else ''
    return _fit_subject(outcome, target_bucket, tag)


def handler(event, context):
    solution_bucket = os.environ['SOLUTION_BUCKET']
    report_only = os.environ['REPORT_ONLY'].lower() == 'true'
    safety_threshold = int(os.environ['SAFETY_THRESHOLD'])
    sns_topic_arn = os.environ['SNS_TOPIC_ARN']
    batch_operations_role_arn = os.environ['BATCH_OPERATIONS_ROLE_ARN']
    target_bucket_env = os.environ['TARGET_BUCKET']
    stack_name = os.environ['STACK_NAME']

    # Account ID/partition derived from the Lambda function ARN (no STS
    # call needed).
    account_id = context.invoked_function_arn.split(':')[4]
    partition = context.invoked_function_arn.split(':')[1]

    # Parse the S3 event record.
    # Key format: manifests/combined/{run_id}/_manifests_ready.json
    record = event['Records'][0]
    raw_key = record['s3']['object']['key']
    key = urllib.parse.unquote_plus(raw_key)
    run_id = key.split('/')[2]

    if not _RUN_ID_PATTERN.match(run_id):
        raise ValueError(
            f"Invalid run_id '{run_id}' in key '{key}'; "
            "expected a hex-encoded S3 event sequencer"
        )

    # Read once per run, ahead of the per-mode loop, and logged whatever
    # the answer is. A run that creates jobs normally still records which
    # SDK it used, so a later failure can be told apart from a runtime
    # that changed underneath the stack.
    capability = _event_hold_capability()
    print(json.dumps({
        'event': 'createjob_event_hold_capability',
        'run_id': run_id,
        **capability,
    }))

    s3 = boto3.client('s3')
    s3control = boto3.client('s3control')
    sns = boto3.client('sns')
    athena = boto3.client('athena')

    # S3 event notifications are at-least-once, not exactly-once -- this
    # Lambda can and does get invoked more than once for the same
    # _manifests_ready.json write (confirmed against a real deployed
    # stack: two invocations, same run_id, both completing successfully).
    # create_job's ClientRequestToken already makes the BOPS job itself
    # idempotent against this, but nothing previously stopped a second
    # invocation from running the whole handler again and publishing a
    # second, duplicate SNS notification for the same run. Claim the run
    # via a conditional PutObject (IfNoneMatch='*') before doing any
    # other work: only the first invocation to reach this line succeeds;
    # every subsequent invocation for the same run_id gets
    # PreconditionFailed and returns immediately, before Athena
    # verification, create_job, or sns.publish run again.
    try:
        s3.put_object(
            Bucket=solution_bucket,
            Key=f'manifests/combined/{run_id}/_createjob_lock.json',
            Body=json.dumps({
                'run_id': run_id,
                'request_id': context.aws_request_id,
            }),
            ContentType='application/json',
            IfNoneMatch='*',
        )
    except ClientError as exc:
        if exc.response['Error']['Code'] == 'PreconditionFailed':
            print(json.dumps({
                'event': 'createjob_duplicate_invocation_skipped',
                'run_id': run_id,
                'request_id': context.aws_request_id,
            }))
            return {'run_id': run_id, 'skipped': 'duplicate_invocation'}
        raise

    # Download and parse the trigger file written by ManifestMaker.
    obj = s3.get_object(Bucket=solution_bucket, Key=key)
    trigger = json.loads(obj['Body'].read())

    # Validate trigger schema and mode values (Req 5.1, 5.2).
    for field_name, expected_type in [
        ('run_id', str), ('dt', str), ('target_bucket', str),
        ('modes_with_manifests', list), ('manifest_keys', dict),
        ('row_counts', dict), ('count_query_execution_ids', dict),
        ('count_query_hashes', dict),
    ]:
        value = trigger.get(field_name)
        if value is None:
            raise ValueError(
                f"Trigger file '{key}' missing required field '{field_name}'"
            )
        if not isinstance(value, expected_type):
            raise ValueError(
                f"Trigger file '{key}' field '{field_name}' has type "
                f"{type(value).__name__}, expected {expected_type.__name__}"
            )
    for mode in trigger.get('modes_with_manifests', []):
        if mode not in _ALLOWED_MODES:
            raise ValueError(
                f"Trigger file '{key}' contains disallowed mode '{mode}'; "
                f"allowed: {sorted(_ALLOWED_MODES)}"
            )

    # Re-validate dt's format (defense in depth — see _DT_PATTERN comment
    # above) and cross-check the trigger's self-reported run_id against
    # the run_id derived from the S3 event key that actually triggered
    # this invocation. A mismatch means the trigger file's content
    # disagrees with the path it was delivered to, which should never
    # happen for a legitimate ManifestMaker-written file.
    if not _DT_PATTERN.match(trigger.get('dt', '')):
        raise ValueError(
            f"Trigger file '{key}' has invalid dt "
            f"'{trigger.get('dt', '')}'; expected YYYY-MM-DDThh-mmZ"
        )
    if trigger.get('run_id') != run_id:
        raise ValueError(
            f"Trigger file '{key}' run_id '{trigger.get('run_id')}' does "
            f"not match key-derived run_id '{run_id}'"
        )
    if trigger.get('target_bucket') != target_bucket_env:
        raise ValueError(
            f"Trigger file '{key}' target_bucket "
            f"'{trigger.get('target_bucket')}' does not match the "
            f"deployed TargetBucket '{target_bucket_env}'"
        )

    created_jobs = {}
    suspended_jobs = {}
    skipped_modes = []
    withheld_modes = {}
    verified_counts = {}

    # Verify every mode's dedicated COUNT(*) query before considering any
    # Batch Operations job. Missing, stale, failed, or mismatched count data
    # withholds that mode without incurring job-creation charges.
    for mode in sorted(_ALLOWED_MODES):
        query_id = trigger['count_query_execution_ids'].get(mode)
        expected_hash = trigger['count_query_hashes'].get(mode)
        trigger_count = trigger['row_counts'].get(mode)
        if (
            not query_id or not expected_hash
            or not isinstance(trigger_count, int) or trigger_count < 0
        ):
            withheld_modes[mode] = {
                'reason': 'missing_or_invalid_count_metadata'
            }
            continue

        try:
            row_count = _verify_count_query(
                athena, query_id, expected_hash,
                os.environ['ATHENA_WORKGROUP'],
            )
        except Exception as exc:
            withheld_modes[mode] = {
                'reason': 'count_query_verification_failed',
                'detail': str(exc),
            }
            continue

        if trigger_count != row_count:
            withheld_modes[mode] = {
                'reason': 'count_mismatch',
                'athena_count': row_count,
                'trigger_count': trigger_count,
            }
            continue

        verified_counts[mode] = row_count
        if row_count == 0:
            skipped_modes.append(mode)
            continue

        # In active mode, exceeding SafetyThreshold means no job is created.
        # This is both fail-closed and avoids per-object Batch Operations
        # charges that are incurred when a job is created.
        if not report_only and row_count > safety_threshold:
            withheld_modes[mode] = {
                'reason': 'safety_threshold_exceeded',
                'eligible_count': row_count,
                'safety_threshold': safety_threshold,
            }
            print(json.dumps({
                'event': 'createjob_withheld',
                'run_id': run_id,
                'mode': mode,
                **withheld_modes[mode],
            }))
            continue

        if mode not in trigger['modes_with_manifests']:
            withheld_modes[mode] = {
                'reason': 'positive_count_without_manifest'
            }
            continue

        manifest_key = trigger['manifest_keys'].get(mode)
        expected_prefix = f'manifests/combined/{run_id}/{mode}/'
        if (
            not manifest_key
            or not manifest_key.startswith(expected_prefix)
            or '..' in manifest_key
        ):
            withheld_modes[mode] = {
                'reason': 'invalid_manifest_key',
                'manifest_key': manifest_key,
            }
            continue

        try:
            head = s3.head_object(
                Bucket=solution_bucket,
                Key=manifest_key,
            )
            etag = head['ETag']
        except ClientError as exc:
            err_code = exc.response['Error']['Code']
            if err_code in ('404', 'NoSuchKey'):
                withheld_modes[mode] = {
                    'reason': 'manifest_not_found'
                }
                continue
            raise

        # ConfirmationRequired=true creates the job SUSPENDED -- it never
        # executes unless manually confirmed in the console/API. This is
        # the ReportOnly preview: inspect the suspended job plus the
        # eligibility manifest for what WOULD be released. Confirmed jobs
        # never happen automatically, so a suspended job produces no
        # completion report to read here.
        confirmation_required = report_only
        dt = trigger['dt']
        client_request_token = hashlib.sha256(
            f'{solution_bucket}/{run_id}/{dt}/{mode}'.encode()
        ).hexdigest()

        # Withhold rather than call create_job when the bundled model is
        # known not to declare EventHold. The call would be rejected
        # inside this function either way; stopping here records why in
        # terms a reader can act on, instead of leaving a validation
        # message to be interpreted. Counts have already been verified
        # above, so a withheld run still reports what it found: the
        # outcome reads like a report-only run, without a job to inspect.
        if capability['event_hold_model'] == 'missing':
            withheld_modes[mode] = {
                'reason': 'event_hold_unsupported_by_runtime',
                'botocore_version': capability['botocore_version'],
                'retention_members': capability.get(
                    'retention_members', []
                ),
            }
            print(json.dumps({
                'event': 'createjob_event_hold_unsupported',
                'run_id': run_id,
                'mode': mode,
                **capability,
            }))
            continue

        try:
            response = s3control.create_job(
                AccountId=account_id,
                ClientRequestToken=client_request_token,
                ConfirmationRequired=confirmation_required,
                Operation={
                    # BypassGovernanceRetention is explicitly False:
                    # releasing an event hold computes retain-until-date
                    # = MAX(existing, release time + EventHoldDuration),
                    # which never shortens protection and therefore never
                    # needs a governance bypass (Req 5.3).
                    'S3PutObjectRetention': {
                        'BypassGovernanceRetention': False,
                        'Retention': {
                            'Mode': mode,
                            'EventHold': 'OFF',
                        },
                    }
                },
                Report={
                    'Bucket': f'arn:{partition}:s3:::{solution_bucket}',
                    'Format': 'Report_CSV_20180820',
                    'Enabled': True,
                    'Prefix': f'reports/{run_id}/{mode}/',
                    'ReportScope': 'AllTasks',
                },
                Manifest={
                    'Spec': {
                        'Format': 'S3BatchOperations_CSV_20180820',
                        'Fields': ['Bucket', 'Key', 'VersionId'],
                    },
                    'Location': {
                        'ObjectArn': (
                            f'arn:{partition}:s3:::{solution_bucket}/'
                            f'{manifest_key}'
                        ),
                        'ETag': etag,
                    },
                },
                Priority=10,
                RoleArn=batch_operations_role_arn,
                Tags=[
                    {
                        'Key': 'job-created-by',
                        # Scoped to this stack so a JobCompletionFunction
                        # in a different stack (e.g. another instance of
                        # this solution targeting a different bucket in
                        # the same account) never matches and processes
                        # this job's completion event.
                        'Value': (
                            f'Auto Remove Event Hold Solution:'
                            f'{stack_name}'
                        ),
                    },
                    {'Key': 'run-id', 'Value': run_id},
                ],
            )
        except Exception as exc:
            # ParamValidationError is raised by the SDK before signing,
            # so it means the call was rejected locally against the
            # bundled service model and never reached S3. Separating it
            # from every other create_job failure matters because the two
            # need opposite responses: this one clears itself when the
            # runtime's bundled SDK updates and needs no action, whereas
            # a rejection returned by S3 is about the request or the
            # account and does need one. Reached when the capability
            # check above returned 'unknown'; the check returning
            # 'missing' withholds before this point.
            if isinstance(exc, ParamValidationError):
                withheld_modes[mode] = {
                    'reason': 'event_hold_unsupported_by_runtime',
                    'botocore_version': capability[
                        'botocore_version'
                    ],
                    'detail': str(exc)[:500],
                }
                print(json.dumps({
                    'event': 'createjob_event_hold_unsupported',
                    'run_id': run_id,
                    'mode': mode,
                    **withheld_modes[mode],
                }))
                continue
            withheld_modes[mode] = {
                'reason': 'create_job_failed',
                'detail': str(exc)[:500],
            }
            print(json.dumps({
                'event': 'createjob_create_job_failed',
                'run_id': run_id,
                'mode': mode,
                **withheld_modes[mode],
            }))
            continue

        job_id = response['JobId']
        if confirmation_required:
            suspended_jobs[mode] = job_id
        else:
            created_jobs[mode] = job_id
            # Record this manifest as the most recently released set of
            # versions for this mode, so the next run's eligibility query
            # anti-joins against it (see _build_candidate_ctes). Recorded
            # optimistically right after create_job for a non-suspended
            # job -- BOPS is asynchronous, so this happens before the job
            # actually runs, not after confirmed success. A row whose
            # release fails still has hold='ON', so it remains a
            # candidate; the anti-join only suppresses it for AT MOST one
            # subsequent qualifying run (previous_manifest holds only the
            # latest per-mode manifest, not an accumulating set -- see
            # PreviousManifestTable), after which it re-qualifies.
            # Meanwhile the BOPS completion report (always on, ReportScope
            # AllTasks) surfaces any per-row failures for manual review.
            s3.copy_object(
                Bucket=solution_bucket,
                Key=f'manifests/previous/{mode}/manifest.csv',
                CopySource={
                    'Bucket': solution_bucket, 'Key': manifest_key,
                },
            )

        print(json.dumps({
            'event': 'createjob_job_created',
            'run_id': run_id,
            'mode': mode,
            'job_id': job_id,
            'row_count': row_count,
            'confirmation_required': confirmation_required,
        }))

    decision_id = context.aws_request_id
    history_key = (
        f'manifests/combined/{run_id}/_job_summaries/'
        f'{decision_id}.json'
    )
    human_summary = _build_human_summary(
        run_id, trigger['dt'], target_bucket_env, report_only,
        verified_counts, created_jobs, suspended_jobs,
        withheld_modes, skipped_modes,
    )
    summary = {
        # Plain-English recap, first key so it's the first thing a reader
        # sees at the top of the SNS email body/S3 JSON file -- everything
        # below is the same structured detail as before, unchanged.
        'summary': human_summary,
        'run_id': run_id,
        'decision_id': decision_id,
        'decision_history_key': history_key,
        'dt': trigger['dt'],
        'target_bucket': target_bucket_env,
        'report_only': report_only,
        # Recorded on every run, not only failed ones, so a successful
        # run is evidence of what the runtime's bundled SDK supported at
        # the time it ran.
        'event_hold_model': capability['event_hold_model'],
        'botocore_version': capability['botocore_version'],
        'modes_processed': list(created_jobs) + list(suspended_jobs),
        'eligible_counts': verified_counts,
        'jobs_created': created_jobs,
        'jobs_suspended': suspended_jobs,
        'modes_withheld': withheld_modes,
        'modes_skipped': skipped_modes,
        'safety_threshold': safety_threshold,
        # Informational only -- carried through from StartQuery/ManifestMaker
        # (see README "Timestamp ties fail closed"). Never gates a job
        # decision; query diagnostics/withheld-candidates via Athena
        # (withheld_candidates_history table) for the full listing.
        'withheld_candidate_count': trigger.get(
            'withheld_candidate_count', 0),
    }
    if created_jobs:
        # At least one ACTIVE job was created. Its real outcome is not
        # known yet -- create_job is asynchronous, and the job can take
        # anywhere from seconds to a long time to reach a terminal BOPS
        # state (see JobCompletionFunction below). Sending the summary
        # email now would describe releases that haven't happened yet,
        # so this Lambda does NOT publish to SNS or write
        # _job_summary.json in this case -- it defers both to
        # JobCompletionFunction, once every active job this run created
        # has actually finished. Write a tracking record instead, with
        # everything JobCompletionFunction needs to pick up where this
        # leaves off without re-deriving it from the trigger file.
        active_jobs_record = {
            'run_id': run_id,
            'decision_id': decision_id,
            'dt': trigger['dt'],
            'target_bucket': target_bucket_env,
            'jobs': {
                mode: {'job_id': job_id, 'status': 'pending'}
                for mode, job_id in created_jobs.items()
            },
            'verified_counts': verified_counts,
            'suspended_jobs': suspended_jobs,
            'withheld_modes': withheld_modes,
            'modes_skipped': skipped_modes,
            'safety_threshold': safety_threshold,
            'withheld_candidate_count': trigger.get(
                'withheld_candidate_count', 0),
        }
        s3.put_object(
            Bucket=solution_bucket,
            Key=f'manifests/combined/{run_id}/_active_jobs.json',
            Body=json.dumps(active_jobs_record),
            ContentType='application/json',
        )
        print(json.dumps({
            'event': 'createjob_deferred_to_completion',
            'run_id': run_id,
            'job_ids': list(created_jobs.values()),
        }))
        return active_jobs_record

    # No active job was created this run (every mode was suspended,
    # withheld, or had nothing eligible) -- the outcome is already final,
    # so send the summary immediately exactly as before.
    subject = _build_subject(
        target_bucket_env, report_only, verified_counts, suspended_jobs,
        withheld_modes, skipped_modes,
    )
    sns.publish(
        TopicArn=sns_topic_arn,
        Subject=subject[:100],
        Message=json.dumps(summary, indent=2),
    )
    summary_body = json.dumps(summary)
    s3.put_object(
        Bucket=solution_bucket,
        Key=history_key,
        Body=summary_body,
        ContentType='application/json',
    )
    s3.put_object(
        Bucket=solution_bucket,
        Key=f'manifests/combined/{run_id}/_job_summary.json',
        Body=summary_body,
        ContentType='application/json',
    )
    return summary
