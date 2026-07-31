"""query-governance MCP tool -- read-only summary of active Security Hub
findings + AWS Config compliance state (governance_stack.py). No write
permissions of any kind -- this can only ever describe compliance posture.
"""

import boto3

securityhub = boto3.client("securityhub")
configservice = boto3.client("config")


def handler(event, context):
    findings_resp = securityhub.get_findings(
        Filters={
            "RecordState": [{"Value": "ACTIVE", "Comparison": "EQUALS"}],
            "WorkflowStatus": [{"Value": "NEW", "Comparison": "EQUALS"}],
        },
        MaxResults=20,
    )
    findings = [
        {
            "title": f.get("Title"),
            "severity": f.get("Severity", {}).get("Label"),
            "resource_type": f.get("Resources", [{}])[0].get("Type") if f.get("Resources") else None,
        }
        for f in findings_resp.get("Findings", [])
    ]

    compliance_resp = configservice.describe_compliance_by_config_rule()
    compliance = [
        {"rule": c.get("ConfigRuleName"), "compliance": c.get("Compliance", {}).get("ComplianceType")}
        for c in compliance_resp.get("ComplianceByConfigRules", [])
    ]

    return {"active_findings": findings, "config_rule_compliance": compliance}
