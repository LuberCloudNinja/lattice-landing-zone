"""governance-metrics-publisher -- scheduled every 15 minutes. AWS Config,
Security Hub, and GuardDuty have NO native CloudWatch metric for "findings/
non-compliant-resources over time" (confirmed via research: Config's own
AWS/Config namespace is usage-only; Security Hub and GuardDuty have no
findings-count namespace at all) -- the documented pattern for graphing
this on a dashboard is exactly what this Lambda does: poll each service's
own API and PutMetricData into a custom namespace.

Read-only against Config/Security Hub/GuardDuty; the only write this Lambda
ever makes is cloudwatch:PutMetricData.
"""

import boto3

configservice = boto3.client("config")
securityhub = boto3.client("securityhub")
guardduty = boto3.client("guardduty")
cloudwatch = boto3.client("cloudwatch")

NAMESPACE = "LatticeLab/Governance"


def _non_compliant_config_rules() -> int:
    resp = configservice.describe_compliance_by_config_rule(
        ComplianceTypes=["NON_COMPLIANT"],
    )
    return len(resp.get("ComplianceByConfigRules", []))


def _active_findings_by_severity() -> dict:
    counts = {"CRITICAL": 0, "HIGH": 0, "MEDIUM": 0, "LOW": 0}
    paginator = securityhub.get_paginator("get_findings")
    for page in paginator.paginate(
        Filters={
            "RecordState": [{"Value": "ACTIVE", "Comparison": "EQUALS"}],
            "WorkflowStatus": [{"Value": "NEW", "Comparison": "EQUALS"}],
        },
        PaginationConfig={"MaxItems": 500},
    ):
        for finding in page.get("Findings", []):
            label = finding.get("Severity", {}).get("Label", "")
            if label in counts:
                counts[label] += 1
    return counts


def _guardduty_findings_total() -> int:
    detectors = guardduty.list_detectors().get("DetectorIds", [])
    if not detectors:
        return 0
    resp = guardduty.list_findings(DetectorId=detectors[0], FindingCriteria={"Criterion": {}})
    return len(resp.get("FindingIds", []))


def handler(event, context):
    metric_data = []

    non_compliant = _non_compliant_config_rules()
    metric_data.append({"MetricName": "NonCompliantConfigRules", "Value": non_compliant, "Unit": "Count"})

    severities = _active_findings_by_severity()
    for label, count in severities.items():
        metric_data.append({"MetricName": f"ActiveFindings{label.title()}", "Value": count, "Unit": "Count"})

    gd_total = _guardduty_findings_total()
    metric_data.append({"MetricName": "GuardDutyFindingsTotal", "Value": gd_total, "Unit": "Count"})

    cloudwatch.put_metric_data(Namespace=NAMESPACE, MetricData=metric_data)

    return {"non_compliant_config_rules": non_compliant, "active_findings": severities, "guardduty_findings": gd_total}
