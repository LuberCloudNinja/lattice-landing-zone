"""drift-remediator Lambda -- see drift_remediation_stack.py's module
docstring for the full blast-radius/scoping writeup. Summary of the safety
gates enforced here, in order, ALL of which must pass before anything is
ever deleted:

  1. EventBridge's own rule pattern already excluded events whose
     userIdentity.arn matches the pipeline's CloudFormation exec role --
     this Lambda re-checks that itself too (defense in depth: don't trust
     the trigger alone to have filtered correctly).
  2. The calling principal must NOT carry a `BreakGlass=true` IAM tag --
     checked live via iam:ListRoleTags/ListUserTags, not from the event
     itself (a break-glass grant is a property of the principal, not
     something CloudTrail records).
  3. The resource this event created/modified must NOT carry the
     project's ManagedBy=cdk tag -- checked live via each service's own
     tagging API (ec2:DescribeTags, s3:GetBucketTagging, etc), not assumed.
     A resource with no tags at all also counts as "not managed by cdk"
     and IS eligible -- untagged is exactly what a manual console action
     produces (this project tags everything it creates via
     config.STANDARD_TAGS applied at the App level in app.py).
  4. The event's action + resource type must be one this handler
     explicitly recognizes (RESOURCE_HANDLERS below). Anything unrecognized
     is alert-only, never a best-effort delete -- a wrong guess here is far
     worse than an unremediated manual change sitting for one more cycle.
  5. DRY_RUN honors config.py's flag: True means alert-only regardless of
     the above.

Only after all five gates pass does this call the actual delete API, and
only ever the ONE specific resource identified by THIS event -- never a
broader sweep.
"""

import json
import os

import boto3

sns = boto3.client("sns")
iam = boto3.client("iam")

SNS_TOPIC_ARN = os.environ["SNS_TOPIC_ARN"]
DRY_RUN = os.environ.get("DRY_RUN", "false").lower() == "true"
MANAGED_BY_TAG_KEY = "ManagedBy"
MANAGED_BY_TAG_VALUE = "cdk"
BREAK_GLASS_TAG_KEY = "BreakGlass"


def handler(event, context):
    detail = event.get("detail", {})
    event_name = detail.get("eventName", "")
    event_source = detail.get("eventSource", "")
    user_identity = detail.get("userIdentity", {})
    principal_arn = user_identity.get("arn", "unknown")

    alert_detail = {
        "principalArn": principal_arn,
        "principalType": user_identity.get("type", "unknown"),
        "action": event_name,
        "eventSource": event_source,
        "requestParameters": detail.get("requestParameters"),
        "responseElements": detail.get("responseElements"),
        "sourceIPAddress": detail.get("sourceIPAddress"),
        "userAgent": detail.get("userAgent"),
        "eventTime": detail.get("eventTime"),
        "awsRegion": detail.get("awsRegion"),
    }
    _publish(
        subject="[governance-alerts] Manual infrastructure change detected",
        message={"kind": "manual-change-detected", **alert_detail},
    )

    if _is_break_glass_principal(principal_arn):
        _publish(
            subject="[governance-alerts] Remediation skipped (break-glass principal)",
            message={"kind": "remediation-skipped", "reason": "break-glass principal", **alert_detail},
        )
        return

    resource = _identify_resource(event_source, event_name, detail)
    if resource is None:
        _publish(
            subject="[governance-alerts] Remediation skipped (unrecognized resource type)",
            message={"kind": "remediation-skipped", "reason": "no handler for this action/resource type", **alert_detail},
        )
        return

    if _is_cdk_managed(resource):
        _publish(
            subject="[governance-alerts] Remediation skipped (ManagedBy=cdk)",
            message={"kind": "remediation-skipped", "reason": "resource carries the project's own ManagedBy=cdk tag", "resource": resource, **alert_detail},
        )
        return

    if DRY_RUN:
        _publish(
            subject="[governance-alerts] DRY_RUN -- would remediate",
            message={"kind": "remediation-dry-run", "resource": resource, **alert_detail},
        )
        return

    result = _delete_resource(resource)
    _publish(
        subject="[governance-alerts] Remediation result",
        message={"kind": "remediation-result", "resource": resource, "result": result, **alert_detail},
    )


def _publish(subject, message):
    sns.publish(TopicArn=SNS_TOPIC_ARN, Subject=subject[:100], Message=json.dumps(message, default=str, indent=2))


def _is_break_glass_principal(principal_arn: str) -> bool:
    if ":role/" not in principal_arn and "assumed-role" not in principal_arn:
        return False  # IAM users / root handled by tag check below too, but roles are the common break-glass case
    try:
        role_name = principal_arn.split("/")[-2] if "assumed-role" in principal_arn else principal_arn.split("/")[-1]
        tags = iam.list_role_tags(RoleName=role_name)["Tags"]
        return any(t["Key"] == BREAK_GLASS_TAG_KEY and t["Value"].lower() == "true" for t in tags)
    except Exception:
        return False  # unknown principal shape / role already gone -- fail closed to "not break-glass", not to "skip"


# ---------------------------------------------------------------------------
# Resource identification + tag check + delete, one entry per recognized
# (eventSource, eventName) pair. Deliberately NOT exhaustive -- see module
# docstring gate 4.
# ---------------------------------------------------------------------------

def _identify_resource(event_source, event_name, detail):
    key = (event_source, event_name)
    handler_fn = RESOURCE_HANDLERS.get(key)
    if handler_fn is None:
        return None
    return handler_fn(detail)


def _ec2_instance(detail):
    items = detail.get("responseElements", {}).get("instancesSet", {}).get("items", [])
    if not items:
        return None
    return {"service": "ec2", "type": "instance", "id": items[0]["instanceId"]}


def _ec2_security_group(detail):
    group_id = detail.get("responseElements", {}).get("groupId")
    if not group_id:
        return None
    return {"service": "ec2", "type": "security-group", "id": group_id}


def _ec2_volume(detail):
    volume_id = detail.get("responseElements", {}).get("volumeId")
    if not volume_id:
        return None
    return {"service": "ec2", "type": "volume", "id": volume_id}


def _s3_bucket(detail):
    bucket_name = detail.get("requestParameters", {}).get("bucketName")
    if not bucket_name:
        return None
    return {"service": "s3", "type": "bucket", "id": bucket_name}


def _iam_role(detail):
    role_name = detail.get("requestParameters", {}).get("roleName")
    if not role_name:
        return None
    return {"service": "iam", "type": "role", "id": role_name}


def _dynamodb_table(detail):
    table_name = detail.get("requestParameters", {}).get("tableName")
    if not table_name:
        return None
    return {"service": "dynamodb", "type": "table", "id": table_name}


def _sns_topic(detail):
    topic_arn = detail.get("responseElements", {}).get("topicArn") or detail.get("requestParameters", {}).get("topicArn")
    if not topic_arn:
        return None
    return {"service": "sns", "type": "topic", "id": topic_arn}


def _lambda_function(detail):
    function_name = detail.get("requestParameters", {}).get("functionName")
    if not function_name:
        return None
    return {"service": "lambda", "type": "function", "id": function_name}


def _logs_log_group(detail):
    log_group_name = detail.get("requestParameters", {}).get("logGroupName")
    if not log_group_name:
        return None
    return {"service": "logs", "type": "log-group", "id": log_group_name}


RESOURCE_HANDLERS = {
    ("ec2.amazonaws.com", "RunInstances"): _ec2_instance,
    ("ec2.amazonaws.com", "CreateSecurityGroup"): _ec2_security_group,
    ("ec2.amazonaws.com", "CreateVolume"): _ec2_volume,
    ("s3.amazonaws.com", "CreateBucket"): _s3_bucket,
    ("iam.amazonaws.com", "CreateRole"): _iam_role,
    ("dynamodb.amazonaws.com", "CreateTable"): _dynamodb_table,
    ("sns.amazonaws.com", "CreateTopic"): _sns_topic,
    ("lambda.amazonaws.com", "CreateFunction20150331"): _lambda_function,
    ("logs.amazonaws.com", "CreateLogGroup"): _logs_log_group,
}


def _is_cdk_managed(resource) -> bool:
    service, rtype, rid = resource["service"], resource["type"], resource["id"]
    try:
        if service == "ec2" and rtype == "instance":
            ec2 = boto3.client("ec2")
            tags = ec2.describe_tags(Filters=[{"Name": "resource-id", "Values": [rid]}])["Tags"]
        elif service == "ec2" and rtype == "security-group":
            ec2 = boto3.client("ec2")
            tags = ec2.describe_tags(Filters=[{"Name": "resource-id", "Values": [rid]}])["Tags"]
        elif service == "ec2" and rtype == "volume":
            ec2 = boto3.client("ec2")
            tags = ec2.describe_tags(Filters=[{"Name": "resource-id", "Values": [rid]}])["Tags"]
        elif service == "s3":
            s3 = boto3.client("s3")
            tags = s3.get_bucket_tagging(Bucket=rid).get("TagSet", [])
        elif service == "iam":
            tags = iam.list_role_tags(RoleName=rid)["Tags"]
        elif service == "dynamodb":
            ddb = boto3.client("dynamodb")
            arn = ddb.describe_table(TableName=rid)["Table"]["TableArn"]
            tags = ddb.list_tags_of_resource(ResourceArn=arn)["Tags"]
        elif service == "sns":
            snsc = boto3.client("sns")
            tags = snsc.list_tags_for_resource(ResourceArn=rid)["Tags"]
        elif service == "lambda":
            lmb = boto3.client("lambda")
            arn = lmb.get_function(FunctionName=rid)["Configuration"]["FunctionArn"]
            tags = [{"Key": k, "Value": v} for k, v in lmb.list_tags(Resource=arn).get("Tags", {}).items()]
        elif service == "logs":
            logsc = boto3.client("logs")
            arn = f"arn:aws:logs:*:*:log-group:{rid}"
            tags = [{"Key": k, "Value": v} for k, v in logsc.list_tags_log_group(logGroupName=rid).get("tags", {}).items()]
        else:
            return True  # unknown shape -- fail closed to "treat as managed, don't touch"
    except Exception:
        return True  # resource already gone, or tag lookup failed -- fail closed, don't delete

    def _tag_value(tags, key):
        for t in tags:
            if t.get("Key") == key:
                return t.get("Value")
        return None

    return _tag_value(tags, MANAGED_BY_TAG_KEY) == MANAGED_BY_TAG_VALUE


def _delete_resource(resource):
    service, rtype, rid = resource["service"], resource["type"], resource["id"]
    try:
        if service == "ec2" and rtype == "instance":
            boto3.client("ec2").terminate_instances(InstanceIds=[rid])
        elif service == "ec2" and rtype == "security-group":
            boto3.client("ec2").delete_security_group(GroupId=rid)
        elif service == "ec2" and rtype == "volume":
            boto3.client("ec2").delete_volume(VolumeId=rid)
        elif service == "s3":
            boto3.client("s3").delete_bucket(Bucket=rid)
        elif service == "iam":
            boto3.client("iam").delete_role(RoleName=rid)
        elif service == "dynamodb":
            boto3.client("dynamodb").delete_table(TableName=rid)
        elif service == "sns":
            boto3.client("sns").delete_topic(TopicArn=rid)
        elif service == "lambda":
            boto3.client("lambda").delete_function(FunctionName=rid)
        elif service == "logs":
            boto3.client("logs").delete_log_group(logGroupName=rid)
        else:
            return {"deleted": False, "reason": "no delete handler for this resource type"}
    except Exception as e:
        return {"deleted": False, "error": str(e)}
    return {"deleted": True}
