"""control_tower_stack.py -- Control Tower landing zone + key controls (management account only).

In a real rollout, AWS Control Tower's landing zone -- and the log-archive
and audit accounts it creates -- are set up FIRST via the Control Tower
console/API (it's what actually creates the org root [[OrgStack]] assumes
exists, plus those two accounts), and only THEN would a manifest like
CfnLandingZone.manifest below be codified for repeatable
updates/drift-detection against that already-existing landing zone. This
stack's CfnLandingZone is therefore illustrative of that end state, not a
from-scratch bootstrap -- deploying it against an org with no landing zone
yet will fail; it's meant to represent "this is what the already-set-up
landing zone's config looks like as code."

Delegated-admin pattern (documented here, not created by any resource
below -- delegation is an Organizations-level trust relationship set up
once via `aws organizations register-delegated-administrator`, not a
CloudFormation resource type in this account/region):
  - Security Hub, Config, and GuardDuty are administered from the AUDIT
    account (config.AUDIT_ACCOUNT_ID) -- that account becomes each
    service's "administrator account" org-wide, seeing aggregated findings
    across every member account, while the management account itself stays
    out of day-to-day security operations (least-privilege for the
    management account, the same principle [[SecurityStack]]'s permissions
    boundary applies at the IAM-role level in the member app).
  - A single CloudTrail ORGANIZATION TRAIL (is_organization_trail=True,
    created once from the management account, not per-member-account)
    ships every member account's events to a CENTRALIZED S3 bucket in the
    LOG-ARCHIVE account (config.LOG_ARCHIVE_ACCOUNT_ID) -- so log integrity
    doesn't depend on any single member account (including a compromised
    one) being unable to tamper with or delete its own trail.
"""

from aws_cdk import CfnOutput, Stack, Tags
from aws_cdk import aws_controltower as controltower
from constructs import Construct

import config
from stacks.org_stack import OrgStack

# Well-known AWS Control Tower control identifiers -- a representative
# subset (detective + preventive) rather than the full control catalog.
KEY_CONTROLS = [
    "AWS-GR_MFA_ENABLED_FOR_IAM_CONSOLE_ACCESS",
    "AWS-GR_ENCRYPTED_VOLUMES",
    "AWS-GR_RESTRICT_ROOT_USER_ACCESS_KEYS",
    "AWS-GR_AUDIT_BUCKET_PUBLIC_READ_PROHIBITED",
    "AWS-GR_RESTRICTED_SSH",
]


class ControlTowerStack(Stack):
    """Illustrative CfnLandingZone + a representative set of CfnEnabledControl."""

    def __init__(self, scope: Construct, construct_id: str, *, org_stack: OrgStack, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)
        Tags.of(self).add("Scope", "org-governance")

        landing_zone = controltower.CfnLandingZone(
            self, "LandingZone",
            version="3.3",
            manifest={
                "governedRegions": config.ALLOWED_REGIONS,
                "organizationStructure": {
                    "security": {"name": config.OrgUnit.SECURITY},
                    "sandbox": {"name": config.OrgUnit.WORKLOADS_NONPROD},
                },
                "centralizedLogging": {
                    "accountId": config.LOG_ARCHIVE_ACCOUNT_ID,
                    "configurations": {
                        "loggingBucket": {"retentionDays": 365},
                        "accessLoggingBucket": {"retentionDays": 365},
                    },
                    "enabled": True,
                },
                "securityRoles": {"accountId": config.AUDIT_ACCOUNT_ID},
                "accessManagement": {"enabled": True},
            },
        )

        # Enabled on the Security OU -- these are all detective/preventive
        # controls appropriate for the accounts holding this project's
        # security tooling (the same OU governance_stack.py-equivalent
        # resources in a real per-account rollout would live under).
        security_ou_arn = org_stack.ous[config.OrgUnit.SECURITY].attr_arn
        for i, control_id in enumerate(KEY_CONTROLS):
            controltower.CfnEnabledControl(
                self, f"Control{i}",
                control_identifier=f"arn:aws:controltower:{self.region}::control/{control_id}",
                target_identifier=security_ou_arn,
            ).node.add_dependency(landing_zone)

        CfnOutput(self, "LandingZoneArn", value=landing_zone.attr_landing_zone_identifier)
