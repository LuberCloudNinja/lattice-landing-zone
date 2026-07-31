"""org_stack.py -- AWS Organizations OUs (management account only).

Four top-level OUs, all direct children of the org root (config.ORG_ROOT_ID
-- assumed to already exist; see this app's README.md for why: the org
itself, plus its root, is normally created once via Control Tower's initial
landing-zone setup, not by this app). scp_stack.py attaches its SCPs to
these same OUs by id, exported here as props.
"""

from aws_cdk import CfnOutput, Stack, Tags
from aws_cdk import aws_organizations as org
from constructs import Construct

import config


class OrgStack(Stack):
    """Security / Infrastructure / Workloads-Prod / Workloads-NonProd OUs."""

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)
        Tags.of(self).add("Scope", "org-governance")

        self.ous = {}
        for ou_name in config.ALL_OUS:
            ou = org.CfnOrganizationalUnit(
                self, f"{ou_name.replace('-', '')}Ou",
                name=ou_name,
                parent_id=config.ORG_ROOT_ID,
            )
            self.ous[ou_name] = ou
            CfnOutput(self, f"{ou_name.replace('-', '')}OuId", value=ou.attr_id)
