"""detect-anomalies MCP tool -- read-only latest VPC Flow Logs anomaly
findings, written by sagemaker_stack.py's scheduled RCF batch-transform job
into ANOMALY_FINDINGS_TABLE. Degrades cleanly (rather than erroring) when
config.ENABLE_SAGEMAKER is off, since ANOMALY_FINDINGS_TABLE is then unset.
"""

import os

import boto3
from boto3.dynamodb.conditions import Key

dynamodb = boto3.resource("dynamodb")

ANOMALY_FINDINGS_TABLE = os.environ.get("ANOMALY_FINDINGS_TABLE", "")


def handler(event, context):
    if not ANOMALY_FINDINGS_TABLE:
        return {"enabled": False, "message": "SageMaker anomaly-detection layer is not enabled in this deployment."}

    table = dynamodb.Table(ANOMALY_FINDINGS_TABLE)
    resp = table.query(
        KeyConditionExpression=Key("finding_type").eq("vpc-flow-log-anomaly"),
        ScanIndexForward=False,
        Limit=10,
    )
    findings = [
        {
            "detected_at": item.get("detected_at"),
            "anomaly_score": item.get("anomaly_score"),
            "flow_log_summary": item.get("flow_log_summary"),
        }
        for item in resp.get("Items", [])
    ]
    return {"enabled": True, "recent_findings": findings}
