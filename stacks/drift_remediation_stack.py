"""drift_remediation_stack.py -- "only the pipeline may change infra," made responsive.

Detective control (governance_stack.py's Config rules, Security Hub
findings) tells you something is non-compliant on the NEXT evaluation
cycle. This stack reacts within seconds of a manual change actually
happening: EventBridge watches CloudTrail's management-event stream for
mutating API calls, and a Lambda decides whether to alert-only or
alert-and-delete.

BLAST-RADIUS RISK -- read before touching this file. A Lambda with delete
permissions triggered off "someone made an API call" is, by construction, a
system that can delete infrastructure automatically. The scoping below
exists specifically to bound that risk to "a human manually created
something outside the pipeline," never anything the pipeline itself does:

  1. EventBridge rule pattern excludes events whose userIdentity.arn starts
     with the CDK bootstrap's shared CloudFormation execution role
     (cdk-hnb659fds-cfn-exec-role-{account}-{region}) -- every stack in
     this project, pipeline-driven OR a direct `cdk deploy`/manual
     `aws cloudformation` call made under that role, is invisible to this
     rule at the EventBridge level already.
  2. The Lambda (stacks/assets/drift_remediation/handler.py) re-derives
     the same exclusion itself, plus a live IAM tag check for a
     `BreakGlass=true` tag on the calling principal, plus a live tag check
     for `ManagedBy=cdk` on the RESOURCE the event touched (not the
     principal) -- a resource carrying this project's own standard tag
     (config.STANDARD_TAGS, applied at the App level) is never touched
     regardless of who created it.
  3. The handler only recognizes a small, explicit allow-list of
     (eventSource, eventName) pairs (RESOURCE_HANDLERS in handler.py) --
     anything outside that list is alert-only. There is no generic/
     best-effort delete path.
  4. config.DRY_RUN (default False) short-circuits every remediation to
     alert-only, network-wide, for whenever you want the detection without
     the response.

Net effect: this can only ever delete ONE specific, newly-created,
untagged resource that a non-break-glass principal just created manually
outside the pipeline -- never a sweep, never anything pipeline-managed,
never anything tagged as this project's own.
"""

import aws_cdk as cdk
from aws_cdk import CfnOutput, Duration, RemovalPolicy, Stack, Tags
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_events as events
from aws_cdk import aws_events_targets as targets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_sns as sns
from aws_cdk import aws_sns_subscriptions as subscriptions
from cdk_nag import NagPackSuppression, NagSuppressions
from constructs import Construct

import config
from stacks.network_stack import NetworkStack
from stacks.security_stack import SecurityStack

# CDK's shared per-account/region CloudFormation execution role -- every
# stack deploy in this project (pipeline-driven or a direct manual
# `cdk deploy`/`aws cloudformation` call under the deloitte-admin profile,
# which assumes into this same role for the actual stack operations)
# authenticates through it. This is the "is this the pipeline" signal.
CFN_EXEC_ROLE_PREFIX_TEMPLATE = "arn:aws:sts::{account}:assumed-role/cdk-hnb659fds-cfn-exec-role-{account}-{region}/"

MUTATING_ACTION_PREFIXES = ["Create", "Run", "Put", "Modify", "Delete", "Attach", "Authorize"]


class DriftRemediationStack(Stack):
    """SNS alerts + EventBridge->Lambda auto-remediation for manual (non-pipeline) infra changes."""

    def __init__(self, scope: Construct, construct_id: str, *, network: NetworkStack, security: SecurityStack, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)
        Tags.of(self).add("Layer", config.Layer.REMEDIATION)

        # ------------------------------------------------------------------
        # SNS -- CMK-encrypted, email subscription to config.REMEDIATION_EMAIL.
        # ------------------------------------------------------------------
        self.alerts_topic = sns.Topic(
            self, "GovernanceAlertsTopic",
            topic_name="governance-alerts",
            master_key=security.sns_key,
        )
        self.alerts_topic.add_subscription(subscriptions.EmailSubscription(config.REMEDIATION_EMAIL))

        # ------------------------------------------------------------------
        # Lambda -- VPC-attached (app-vpc Private subnets; egress via the
        # existing NAT-via-inspection path already used by this project's
        # Fargate tasks, not a new public IP), least-privilege role.
        # ------------------------------------------------------------------
        remediator_sg = ec2.SecurityGroup(
            self, "RemediatorSg",
            vpc=network.app_vpc,
            description="drift-remediator Lambda -- outbound only (AWS API calls)",
            allow_all_outbound=True,
        )

        remediator_role = iam.Role(
            self, "RemediatorRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            permissions_boundary=security.permissions_boundary,
            managed_policies=[iam.ManagedPolicy.from_aws_managed_policy_name("service-role/AWSLambdaVPCAccessExecutionRole")],
        )
        self.alerts_topic.grant_publish(remediator_role)
        # Read-only tag/describe calls needed to evaluate gates 2-3 in the
        # module docstring, PLUS the narrow, explicit delete permissions
        # matching handler.py's RESOURCE_HANDLERS allow-list -- deliberately
        # not a wildcard *:Delete*.
        remediator_role.add_to_policy(iam.PolicyStatement(
            sid="ReadForGating",
            effect=iam.Effect.ALLOW,
            actions=[
                "ec2:DescribeTags", "ec2:DescribeInstances", "ec2:DescribeSecurityGroups", "ec2:DescribeVolumes",
                "s3:GetBucketTagging",
                "iam:ListRoleTags", "iam:ListUserTags", "iam:GetRole",
                "dynamodb:DescribeTable", "dynamodb:ListTagsOfResource",
                "sns:ListTagsForResource",
                "lambda:GetFunction", "lambda:ListTags",
                "logs:ListTagsLogGroup",
            ],
            resources=["*"],
        ))
        remediator_role.add_to_policy(iam.PolicyStatement(
            sid="ScopedRemediation",
            effect=iam.Effect.ALLOW,
            actions=[
                "ec2:TerminateInstances", "ec2:DeleteSecurityGroup", "ec2:DeleteVolume",
                "s3:DeleteBucket",
                "iam:DeleteRole",
                "dynamodb:DeleteTable",
                "sns:DeleteTopic",
                "lambda:DeleteFunction",
                "logs:DeleteLogGroup",
            ],
            resources=["*"],
        ))

        remediator_log_group = logs.LogGroup(
            self, "RemediatorLogGroup",
            retention=logs.RetentionDays.ONE_MONTH,
            encryption_key=security.logs_key,
            removal_policy=RemovalPolicy.DESTROY,
        )
        self.remediator_fn = lambda_.Function(
            self, "DriftRemediator",
            function_name="drift-remediator",
            runtime=lambda_.Runtime.PYTHON_3_13,
            handler="handler.handler",
            code=lambda_.Code.from_asset("stacks/assets/drift_remediation"),
            timeout=Duration.seconds(60),
            role=remediator_role,
            vpc=network.app_vpc,
            vpc_subnets=ec2.SubnetSelection(subnet_group_name="Private"),
            security_groups=[remediator_sg],
            log_group=remediator_log_group,
            environment={
                "SNS_TOPIC_ARN": self.alerts_topic.topic_arn,
                "DRY_RUN": "true" if config.DRY_RUN else "false",
            },
        )

        # ------------------------------------------------------------------
        # EventBridge -- coarse filter (see module docstring gate 1).
        # ------------------------------------------------------------------
        cfn_exec_role_prefix = CFN_EXEC_ROLE_PREFIX_TEMPLATE.format(account=self.account, region=self.region)
        rule = events.Rule(
            self, "ManualMutationRule",
            description="Mutating API calls NOT made by this project's own CloudFormation execution role.",
            event_pattern=events.EventPattern(
                detail_type=["AWS API Call via CloudTrail"],
                detail={
                    "eventName": [{"prefix": p} for p in MUTATING_ACTION_PREFIXES],
                    "userIdentity": {
                        "arn": [{"anything-but": {"prefix": cfn_exec_role_prefix}}],
                    },
                },
            ),
        )
        rule.add_target(targets.LambdaFunction(self.remediator_fn))

        CfnOutput(self, "GovernanceAlertsTopicArn", value=self.alerts_topic.topic_arn)
        CfnOutput(self, "DriftRemediatorFunctionName", value=self.remediator_fn.function_name)
        CfnOutput(self, "DryRunEnabled", value=str(config.DRY_RUN))

        # ------------------------------------------------------------------
        # cdk-nag suppressions (SPEC.md Section 10).
        # ------------------------------------------------------------------
        NagSuppressions.add_stack_suppressions(self, [
            NagPackSuppression(
                id="AwsSolutions-IAM4",
                reason="service-role/AWSLambdaVPCAccessExecutionRole on RemediatorRole is the AWS-curated "
                       "policy required for any VPC-attached Lambda to create/manage its ENIs -- no "
                       "narrower alternative exists for that specific capability.",
            ),
            NagPackSuppression(
                id="AwsSolutions-IAM5",
                reason="ReadForGating/ScopedRemediation use resource='*' because the resource this "
                       "Lambda acts on is determined at RUNTIME from the CloudTrail event it receives "
                       "(module docstring gate 3 explains why this can't be scoped at synth time) -- the "
                       "real scoping control is the explicit action allow-list (no wildcard actions) plus "
                       "the tag-based gates in handler.py, not IAM resource ARNs.",
            ),
            NagPackSuppression(
                id="AwsSolutions-L1",
                reason="PYTHON_3_13 is the latest available managed runtime as of this build.",
            ),
        ])
