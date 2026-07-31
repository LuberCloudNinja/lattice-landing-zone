"""scp_stack.py -- preventive guardrails (management account only).

Six Service Control Policies, the preventive counterpart to
[[GovernanceStack]]/[[DriftRemediationStack]]'s detective controls in the
member-account app: those react to a change after CloudTrail records it;
these stop the API call from succeeding at all, for any account under the
targeted OUs (SCPs bound the maximum available permissions -- an explicit
Allow in a member account's own IAM can never override a Deny here).

Five baseline guardrails attach to every OU in [[OrgStack]] (Security,
Infrastructure, Workloads-Prod, Workloads-NonProd): don't disable the
detection plane, no root-user actions, stay in the two approved regions,
IMDSv2 only, no public S3. The sixth -- "pipeline-only" -- attaches ONLY to
the two Workloads OUs, the preventive twin of drift_remediation_stack.py's
detective Lambda in the member app: it denies the same mutating-action
prefixes unless the caller is the CDK bootstrap's per-account
cfn-exec role (arn:...:role/cdk-hnb659fds-cfn-exec-role-*, the same
principal pattern drift_remediation_stack.py's EventBridge rule excludes)
or carries a BreakGlass=true principal tag (aws:PrincipalTag/BreakGlass,
a real SCP condition key -- this is where "tagged break-glass principal"
becomes enforceable, not just detectable, since evaluating IAM principal
tags. Security/Infrastructure OUs are deliberately excluded from this one:
those hold security-tooling/log-archive/audit accounts, which aren't
managed by this project's CDK pipeline and shouldn't be constrained to it.
"""

from aws_cdk import CfnOutput, Stack, Tags
from aws_cdk import aws_organizations as org
from constructs import Construct

import config
from stacks.org_stack import OrgStack

DENY_DISABLE_SECURITY_SERVICES = {
    "Version": "2012-10-17",
    "Statement": [{
        "Sid": "DenyDisablingDetectionPlane",
        "Effect": "Deny",
        "Action": [
            "cloudtrail:StopLogging", "cloudtrail:DeleteTrail", "cloudtrail:UpdateTrail",
            "config:StopConfigurationRecorder", "config:DeleteConfigurationRecorder", "config:DeleteDeliveryChannel",
            "guardduty:DeleteDetector", "guardduty:UpdateDetector", "guardduty:DisassociateFromMasterAccount",
            "securityhub:DisableSecurityHub", "securityhub:DeleteHub", "securityhub:UpdateStandardsControl",
        ],
        "Resource": "*",
    }],
}

DENY_ROOT_ACTIONS = {
    "Version": "2012-10-17",
    "Statement": [{
        "Sid": "DenyRootUser",
        "Effect": "Deny",
        "Action": "*",
        "Resource": "*",
        "Condition": {"StringLike": {"aws:PrincipalArn": ["arn:aws:iam::*:root"]}},
    }],
}

DENY_UNAPPROVED_REGIONS = {
    "Version": "2012-10-17",
    "Statement": [{
        "Sid": "DenyOutsideApprovedRegions",
        "Effect": "Deny",
        # Standard region-restriction SCP exemption list -- global/
        # account-level services with no meaningful "region" of their own.
        "NotAction": [
            "iam:*", "organizations:*", "route53:*", "route53domains:*", "cloudfront:*",
            "support:*", "budgets:*", "sts:*", "trustedadvisor:*", "waf:*", "wafv2:*",
            "health:*", "networkmanager:*", "shield:*", "chime:*", "globalaccelerator:*",
        ],
        "Resource": "*",
        "Condition": {"StringNotEquals": {"aws:RequestedRegion": config.ALLOWED_REGIONS}},
    }],
}

REQUIRE_IMDSV2 = {
    "Version": "2012-10-17",
    "Statement": [{
        "Sid": "DenyEc2LaunchWithoutImdsv2",
        "Effect": "Deny",
        "Action": "ec2:RunInstances",
        "Resource": "arn:aws:ec2:*:*:instance/*",
        "Condition": {"StringNotEquals": {"ec2:MetadataHttpTokens": "required"}},
    }],
}

DENY_PUBLIC_S3 = {
    "Version": "2012-10-17",
    "Statement": [
        {
            "Sid": "DenyDisablingAccountPublicAccessBlock",
            "Effect": "Deny",
            "Action": "s3:PutAccountPublicAccessBlock",
            "Resource": "*",
            "Condition": {"Bool": {
                "s3:PublicAccessBlockConfiguration:BlockPublicAcls": "false",
                "s3:PublicAccessBlockConfiguration:BlockPublicPolicy": "false",
            }},
        },
        {
            "Sid": "DenyPublicReadWriteAcls",
            "Effect": "Deny",
            "Action": ["s3:PutBucketAcl", "s3:PutObjectAcl"],
            "Resource": "*",
            "Condition": {"StringEquals": {"s3:x-amz-acl": ["public-read", "public-read-write"]}},
        },
    ],
}


def _pipeline_only_policy() -> dict:
    return {
        "Version": "2012-10-17",
        "Statement": [{
            "Sid": "DenyMutationsOutsidePipeline",
            "Effect": "Deny",
            "Action": [
                "ec2:Create*", "ec2:Run*", "ec2:Modify*", "ec2:Delete*", "ec2:Attach*", "ec2:Authorize*",
                "s3:Create*", "s3:Put*", "s3:Delete*",
                "iam:Create*", "iam:Put*", "iam:Delete*", "iam:Attach*",
                "dynamodb:Create*", "dynamodb:Delete*", "dynamodb:Update*",
                "ecs:Create*", "ecs:Delete*", "ecs:Update*", "ecs:Run*",
                "lambda:Create*", "lambda:Delete*", "lambda:Update*",
            ],
            "Resource": "*",
            "Condition": {
                "StringNotLike": {"aws:PrincipalArn": ["arn:aws:iam::*:role/cdk-hnb659fds-cfn-exec-role-*"]},
                "StringNotEqualsIfExists": {"aws:PrincipalTag/BreakGlass": "true"},
            },
        }],
    }


class ScpStack(Stack):
    """Six SCPs, attached to OrgStack's OUs."""

    def __init__(self, scope: Construct, construct_id: str, *, org_stack: OrgStack, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)
        Tags.of(self).add("Scope", "org-governance")

        all_ou_ids = [ou.attr_id for ou in org_stack.ous.values()]
        workload_ou_ids = [org_stack.ous[n].attr_id for n in (config.OrgUnit.WORKLOADS_PROD, config.OrgUnit.WORKLOADS_NONPROD)]

        baseline_policies = {
            "DenyDisableSecurityServices": DENY_DISABLE_SECURITY_SERVICES,
            "DenyRootActions": DENY_ROOT_ACTIONS,
            "DenyUnapprovedRegions": DENY_UNAPPROVED_REGIONS,
            "RequireImdsv2": REQUIRE_IMDSV2,
            "DenyPublicS3": DENY_PUBLIC_S3,
        }
        for policy_id, content in baseline_policies.items():
            policy = org.CfnPolicy(
                self, policy_id,
                name=f"lattice-lab-{policy_id}",
                type="SERVICE_CONTROL_POLICY",
                content=content,
                target_ids=all_ou_ids,
            )
            CfnOutput(self, f"{policy_id}PolicyId", value=policy.attr_id)

        pipeline_only = org.CfnPolicy(
            self, "PipelineOnlyMutation",
            name="lattice-lab-PipelineOnlyMutation",
            type="SERVICE_CONTROL_POLICY",
            content=_pipeline_only_policy(),
            target_ids=workload_ou_ids,
        )
        CfnOutput(self, "PipelineOnlyMutationPolicyId", value=pipeline_only.attr_id)
