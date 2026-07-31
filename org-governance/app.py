#!/usr/bin/env python3
"""org-governance app entry point -- MANAGEMENT ACCOUNT ONLY.

Deliberately a separate CDK app from the rest of this repo, not a stack
inside LandingZoneStage or deployed via the member-account CDK Pipeline --
see this directory's README.md for why (Organizations/Control Tower
resources only exist in a management account; the member app has no access
to them, and mixing the two would mean the member pipeline's role would
need Organizations-admin permissions it should never hold).

Deploy by hand, from a management-account session:
    cd org-governance && pip install -r requirements.txt
    ORG_ROOT_ID=r-xxxx LOG_ARCHIVE_ACCOUNT_ID=... AUDIT_ACCOUNT_ID=... \\
        cdk deploy --all --profile <management-account-profile>
"""

import aws_cdk as cdk
from cdk_nag import AwsSolutionsChecks

import config
from stacks.control_tower_stack import ControlTowerStack
from stacks.org_stack import OrgStack
from stacks.scp_stack import ScpStack

app = cdk.App()
env = cdk.Environment(account=config.AWS_ACCOUNT_ID, region=config.AWS_REGION)

org_stack = OrgStack(app, "OrgStack", env=env)
ScpStack(app, "ScpStack", org_stack=org_stack, env=env)
ControlTowerStack(app, "ControlTowerStack", org_stack=org_stack, env=env)

cdk.Aspects.of(app).add(AwsSolutionsChecks(verbose=True))

for key, value in config.STANDARD_TAGS.items():
    cdk.Tags.of(app).add(key, value)

app.synth()
