"""Constants for the org-governance app -- MANAGEMENT ACCOUNT ONLY.

Separate from ../config.py deliberately: this app deploys resources
(AWS::Organizations::*, AWS::ControlTower::*) that only exist -- and can
only be created -- in an Organizations management account. The
lattice-landing-zone member app (the rest of this repo) runs in a standalone
member account and has no Organizations API access at all, which is exactly
why this is its own app rather than a stack added to LandingZoneStage.
"""

import os

AWS_ACCOUNT_ID = os.environ.get("CDK_DEFAULT_ACCOUNT", "REPLACE_ME_MANAGEMENT_ACCOUNT_ID")
AWS_REGION = os.environ.get("CDK_DEFAULT_REGION", "us-east-1")

# The Organization root's id (looked up via `aws organizations list-roots`
# once the org exists) -- every top-level OU below is created as a direct
# child of this. No CDK/CloudFormation resource creates the Organization
# itself or looks this up automatically in this app; it's assumed to
# already exist (this project's org-governance/README.md documents the
# Control Tower prerequisite this implies).
# Default is a syntactically-valid-but-fake root id ("r-0000") so this app
# still synths cleanly (CloudFormation's own schema validation checks
# ParentId's format at synth time, not just deploy time) -- MUST be
# overridden with the real root id (`aws organizations list-roots`) before
# ever actually deploying.
ORG_ROOT_ID = os.environ.get("ORG_ROOT_ID", "r-0000")

# Log-archive / audit account ids -- the delegated-admin targets referenced
# by scp_stack.py's comments and org-governance/README.md. Normally created
# BY Control Tower's landing zone setup (log-archive, audit) rather than by
# this app; filled in here only as documentation/reference, not consumed by
# any resource property (nothing in this app grants access to them, since
# doing so safely requires knowing they actually exist first).
LOG_ARCHIVE_ACCOUNT_ID = os.environ.get("LOG_ARCHIVE_ACCOUNT_ID", "REPLACE_ME_LOG_ARCHIVE_ACCOUNT_ID")
AUDIT_ACCOUNT_ID = os.environ.get("AUDIT_ACCOUNT_ID", "REPLACE_ME_AUDIT_ACCOUNT_ID")

ALLOWED_REGIONS = ["us-east-1", "us-east-2"]

STANDARD_TAGS = {
    "Project": "lattice-lab",
    "Environment": "lab",
    "ManagedBy": "cdk",
    "Scope": "org-governance",
}


class OrgUnit:
    SECURITY = "Security"
    INFRASTRUCTURE = "Infrastructure"
    WORKLOADS_PROD = "Workloads-Prod"
    WORKLOADS_NONPROD = "Workloads-NonProd"


ALL_OUS = [OrgUnit.SECURITY, OrgUnit.INFRASTRUCTURE, OrgUnit.WORKLOADS_PROD, OrgUnit.WORKLOADS_NONPROD]
