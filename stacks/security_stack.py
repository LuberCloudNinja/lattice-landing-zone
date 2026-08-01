"""security_stack.py -- single source of truth for identity + keys.

Built FIRST in stage.py: every other stack imports this stack's CMKs and
permissions boundary as constructor props (SPEC.md Section 3: "not by
re-lookup"), so nothing downstream can synth without it.

Five single-purpose CMKs (logs, buckets, ebs, secrets, sns) rather than one
shared key -- purpose separation is itself a form of least privilege (a
compromised grant on `sns_key` can't touch `secrets_key`), and it lets each
key's policy grant only the one AWS service principal that actually needs it.

Permissions boundary: a customer-managed policy capped with an explicit Deny
on privilege-escalation vectors (iam:CreateRole/PutRolePolicy/AttachRolePolicy/
etc, and removing the boundary from itself) and account/org-level actions,
layered on top of a curated Allow list for what this project's roles
actually do (SSM, ECS/ECR, CloudWatch, the project's own DynamoDB table,
VPC Lattice, and KMS usage -- not management -- of the five CMKs above).

Scope decision, stated plainly: the boundary is applied via explicit
`permissions_boundary=` props passed into the roles THIS project's code
authors (network_stack.py / inspection_stack.py / threetier_stack.py /
privatelink_stack.py's iam.Role(...) calls, refactored in the same pass that
wires this stack in), not as a blanket `iam.PermissionsBoundary.of(app)`
Aspect over the whole App/Stage. An Aspect would also retroactively catch
every IAM role CDK creates internally for its own plumbing (BucketDeployment,
AwsCustomResource providers, log-retention custom resources -- there are
several of these across this project, e.g. lattice_stack.py's
SelfSignedCertImport and LatticeManagedPrefixListLookup) whose exact runtime
IAM needs aren't enumerated by this project's code and aren't verifiable via
`cdk synth` alone (a boundary that's too narrow for one of those would only
surface as a runtime AccessDenied deep into a real deploy). Scoping to
explicitly-authored roles gets real boundary coverage on every role this
project actually designs, without gambling the whole already-working
deployment on an unverified blanket change.
"""

import aws_cdk as cdk
from aws_cdk import CfnOutput, Duration, RemovalPolicy, Stack, Tags
from aws_cdk import aws_accessanalyzer as accessanalyzer
from aws_cdk import aws_iam as iam
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_kms as kms
from cdk_nag import NagPackSuppression, NagSuppressions
from constructs import Construct

import config


class SecurityStack(Stack):
    """CMKs + permissions boundary + shared instance role, exported as props."""

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)
        Tags.of(self).add("Layer", config.Layer.SECURITY)

        # ------------------------------------------------------------------
        # CMKs -- one per purpose, each with rotation on and a key policy
        # scoped to (a) the account root (AWS's own recommended default so
        # IAM policies/grants keep working) and (b) the one AWS service
        # principal that actually needs to use it.
        # ------------------------------------------------------------------
        self.logs_key = self._make_key(
            "LogsKey", "logs", "logs.amazonaws.com",
            "CloudWatch Logs group encryption (governance_stack.py's CloudTrail "
            "log group, observability_stack.py's Suricata/nftables groups, "
            "drift_remediation_stack.py's Lambda log group).",
        )
        self.buckets_key = self._make_key(
            "BucketsKey", "buckets", "s3.amazonaws.com",
            "S3 bucket SSE-KMS encryption for every bucket in this project "
            "(web, policy, access-logs, diagram, CloudTrail, Config).",
        )
        self.ebs_key = self._make_key(
            "EbsKey", "ebs", "ec2.amazonaws.com",
            "EBS root-volume encryption for every EC2 instance/launch "
            "template in this project.",
        )
        self.secrets_key = self._make_key(
            "SecretsKey", "secrets", "secretsmanager.amazonaws.com",
            "Reserved for Secrets Manager use -- nothing in this project "
            "currently stores a secret (the RDS secret was removed when "
            "threetier_stack.py swapped to DynamoDB), exported as a prop "
            "for any future stack that needs one.",
        )
        self.sns_key = self._make_key(
            "SnsKey", "sns", "sns.amazonaws.com",
            "SNS topic encryption (drift_remediation_stack.py's "
            "governance-alerts topic).",
        )

        # ------------------------------------------------------------------
        # Shared S3 server-access-log destination -- every other bucket in
        # this project (web, policy, lattice access-logs, diagram, plus
        # governance_stack.py's CloudTrail/Config buckets) points its own
        # `server_access_logs_bucket` at this one. Deliberately SSE-S3
        # (S3_MANAGED), not the buckets CMK: this is an S3 Log Delivery
        # Group destination, and CDK's bucket-policy-based log delivery is
        # the well-trodden path with SSE-S3; adding SSE-KMS here would only
        # add key-policy/grant surface for a bucket that holds nothing more
        # sensitive than access-pattern metadata already scoped BLOCK_ALL +
        # enforce_ssl. A bucket can't log to itself, so this one has no
        # server_access_logs_bucket of its own -- that's the one deliberate
        # exception to "every bucket gets access logging" in this project.
        # ------------------------------------------------------------------
        self.s3_access_logs_bucket = s3.Bucket(
            self, "S3AccessLogsBucket",
            versioned=True,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
        )

        # ------------------------------------------------------------------
        # Permissions boundary -- curated Allow for what this project's
        # roles actually do, capped with an explicit Deny on privilege
        # escalation and account/org-level actions. See module docstring
        # for where/how this gets attached.
        # ------------------------------------------------------------------
        cmk_arns = [k.key_arn for k in (
            self.logs_key, self.buckets_key, self.ebs_key, self.secrets_key, self.sns_key,
        )]
        self.permissions_boundary = iam.ManagedPolicy(
            self, "PermissionsBoundary",
            managed_policy_name="lattice-lab-permissions-boundary",
            document=iam.PolicyDocument(statements=[
                iam.PolicyStatement(
                    sid="AllowProjectOperations",
                    effect=iam.Effect.ALLOW,
                    actions=[
                        "ec2:Describe*", "ec2:CreateTags",
                        # ENI lifecycle for VPC-attached Lambdas (AWSLambdaVPCAccessExecutionRole
                        # grants these on the identity-policy side, but the boundary is a hard
                        # ceiling -- confirmed live via drift_remediation_stack.py's Lambda
                        # failing to create with "does not have permissions to call
                        # CreateNetworkInterface on EC2" until these were added here too).
                        "ec2:CreateNetworkInterface", "ec2:DeleteNetworkInterface", "ec2:DetachNetworkInterface",
                        "ec2:AssignPrivateIpAddresses", "ec2:UnassignPrivateIpAddresses",
                        "ssm:*", "ssmmessages:*", "ec2messages:*",
                        "logs:*", "cloudwatch:PutMetricData", "cloudwatch:GetMetricData",
                        "ecr:GetAuthorizationToken", "ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer",
                        "ecs:*",
                        "dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem",
                        "dynamodb:DeleteItem", "dynamodb:Query", "dynamodb:Scan",
                        "vpc-lattice:*",
                        "elasticloadbalancing:Describe*",
                        "s3:GetObject", "s3:PutObject", "s3:ListBucket",
                        # drift_remediation_stack.py's handler.py: read-for-gating (tag
                        # checks on the resource an event touched, plus the calling
                        # principal's own tags) and scoped-remediation (delete) actions --
                        # see that stack's ReadForGating/ScopedRemediation statements.
                        "ec2:TerminateInstances", "ec2:DeleteSecurityGroup", "ec2:DeleteVolume",
                        "s3:DeleteBucket", "s3:GetBucketTagging",
                        "iam:ListRoleTags", "iam:ListUserTags", "iam:GetRole", "iam:DeleteRole",
                        "dynamodb:DescribeTable", "dynamodb:ListTagsOfResource", "dynamodb:DeleteTable",
                        "sns:Publish", "sns:ListTagsForResource", "sns:DeleteTopic",
                        "lambda:GetFunction", "lambda:ListTags", "lambda:DeleteFunction",
                        # agentic_ai_stack.py / sagemaker_stack.py / auto_heal_stack.py's own
                        # actual runtime needs -- same "the boundary is a hard ceiling, add
                        # what each role actually calls" lesson as the ENI actions above.
                        # Confirmed live: SemanticMemoryKnowledgeBase CREATE_FAILED with
                        # "no identity-based policy allows the s3vectors:QueryVectors action"
                        # despite KnowledgeBaseRole's own identity policy granting it.
                        "bedrock:InvokeModel", "bedrock-agent-runtime:Retrieve",
                        "s3vectors:GetVectors", "s3vectors:PutVectors", "s3vectors:QueryVectors", "s3vectors:GetIndex",
                        "securityhub:GetFindings", "config:DescribeComplianceByConfigRule",
                        "networkmanager:GetCoreNetwork", "networkmanager:ListAttachments",
                        "codecommit:GetBranch", "codecommit:CreateBranch", "codecommit:CreateCommit", "codecommit:CreatePullRequest",
                        "sagemaker:CreateModel", "sagemaker:CreateEndpointConfig", "sagemaker:CreateEndpoint", "sagemaker:UpdateEndpoint",
                        "sagemaker:DescribeEndpoint", "sagemaker:DescribeEndpointConfig", "sagemaker:DeleteModel", "sagemaker:DeleteEndpointConfig",
                        "sagemaker:DeleteEndpoint", "sagemaker:ListEndpoints", "sagemaker:ListEndpointConfigs", "sagemaker:ListModels",
                        "sagemaker:CreateTrainingJob", "sagemaker:CreateTransformJob",
                        "application-autoscaling:RegisterScalableTarget", "application-autoscaling:PutScalingPolicy",
                        "application-autoscaling:DeregisterScalableTarget",
                        "ec2:RebootInstances",
                        # Ground-truth audit round 2: the first audit only scanned this
                        # project's own literal actions=[...] lists, which missed every
                        # action a CDK .grant_*() helper generates internally (e.g.
                        # fn.grant_invoke() -> lambda:InvokeFunction, TableV2.
                        # grant_read_write_data() -> the dynamodb:Batch*/GetRecords/
                        # GetShardIterator/ConditionCheckItem actions, bucket.grant_read_
                        # write() -> the s3:GetObject*/GetBucket*/List*/DeleteObject*/
                        # PutObject{LegalHold,Retention,Tagging,VersionTagging} actions,
                        # state_machine.grant_start_execution() -> states:StartExecution).
                        # Found by diffing the boundary against the ACTUAL synthesized
                        # AWS::IAM::Policy resources across agentic_ai_stack.py/
                        # sagemaker_stack.py/auto_heal_stack.py, not by re-reading source.
                        # Confirmed live: every GatewayTarget CREATE_FAILED with "Gateway
                        # execution role lacks permission to invoke Lambda function" even
                        # with both an identity-policy grant AND a Lambda resource policy
                        # in place -- the boundary's missing lambda:InvokeFunction was the
                        # real remaining blocker. xray:* is X-Ray tracing on
                        # AutoHealStack's state machine (tracing_enabled=True).
                        "dynamodb:BatchGetItem", "dynamodb:BatchWriteItem", "dynamodb:ConditionCheckItem",
                        "dynamodb:GetRecords", "dynamodb:GetShardIterator",
                        "lambda:InvokeFunction",
                        "s3:Abort*", "s3:DeleteObject*", "s3:GetBucket*", "s3:GetObject*", "s3:List*",
                        "s3:PutObjectLegalHold", "s3:PutObjectRetention", "s3:PutObjectTagging", "s3:PutObjectVersionTagging",
                        "states:StartExecution",
                        "xray:GetSamplingRules", "xray:GetSamplingTargets", "xray:PutTelemetryRecords", "xray:PutTraceSegments",
                    ],
                    resources=["*"],
                ),
                iam.PolicyStatement(
                    sid="AllowScopedPassRoleToSageMaker",
                    effect=iam.Effect.ALLOW,
                    actions=["iam:PassRole"],
                    resources=["*"],
                    conditions={"StringEquals": {"iam:PassedToService": "sagemaker.amazonaws.com"}},
                ),
                iam.PolicyStatement(
                    sid="AllowProjectKmsUsage",
                    effect=iam.Effect.ALLOW,
                    actions=["kms:Decrypt", "kms:DescribeKey", "kms:GenerateDataKey*", "kms:Encrypt", "kms:ReEncrypt*"],
                    resources=cmk_arns,
                ),
                iam.PolicyStatement(
                    sid="DenyPrivilegeEscalation",
                    effect=iam.Effect.DENY,
                    actions=[
                        "iam:CreateUser", "iam:CreateRole", "iam:CreatePolicy", "iam:CreatePolicyVersion",
                        "iam:AttachUserPolicy", "iam:AttachRolePolicy", "iam:AttachGroupPolicy",
                        "iam:PutUserPolicy", "iam:PutRolePolicy", "iam:PutGroupPolicy",
                        "iam:UpdateAssumeRolePolicy", "iam:PassRole",
                        "iam:DeleteRolePermissionsBoundary", "iam:DeleteUserPermissionsBoundary",
                        "iam:PutRolePermissionsBoundary", "iam:PutUserPermissionsBoundary",
                    ],
                    # iam:PassRole is excepted when passing to sagemaker.amazonaws.com
                    # specifically (sagemaker_stack.py's Lambdas legitimately pass
                    # SageMakerExecutionRole to CreateTrainingJob/CreateModel/etc --
                    # a standard AWS pattern, not privilege escalation, since it hands
                    # the role to a fixed AWS service rather than another principal).
                    # Explicit Deny always beats Allow, so this condition has to live
                    # on the Deny itself, not just as a separate Allow statement above.
                    conditions={"StringNotEquals": {"iam:PassedToService": "sagemaker.amazonaws.com"}},
                    resources=["*"],
                ),
                iam.PolicyStatement(
                    sid="DenyAccountAndOrgActions",
                    effect=iam.Effect.DENY,
                    actions=["organizations:*", "account:*", "kms:ScheduleKeyDeletion", "kms:DisableKey"],
                    resources=["*"],
                ),
            ]),
        )

        # ------------------------------------------------------------------
        # Shared instance role/profile -- for any stack that just needs a
        # plain SSM-managed EC2 role rather than its own bespoke one.
        # Existing bespoke roles (firewall_role, libreswan_role, etc.) keep
        # their own identities (distinct trust/least-privilege reasoning
        # per role, documented at each call site) but now also take
        # permissions_boundary=security.permissions_boundary -- see the
        # refactor pass in network_stack.py / inspection_stack.py /
        # threetier_stack.py / privatelink_stack.py.
        # ------------------------------------------------------------------
        self.shared_instance_role = iam.Role(
            self, "SharedInstanceRole",
            role_name="lattice-lab-shared-instance-role",
            assumed_by=iam.ServicePrincipal("ec2.amazonaws.com"),
            permissions_boundary=self.permissions_boundary,
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name("AmazonSSMManagedInstanceCore"),
            ],
        )
        self.shared_instance_profile = iam.InstanceProfile(
            self, "SharedInstanceProfile", role=self.shared_instance_role,
        )

        # ------------------------------------------------------------------
        # IAM Access Analyzer -- account-level, flags resource policies
        # that grant access to principals outside the account/org.
        # ------------------------------------------------------------------
        accessanalyzer.CfnAnalyzer(
            self, "AccountAnalyzer",
            analyzer_name="lattice-lab-account-analyzer",
            type="ACCOUNT",
        )

        CfnOutput(self, "PermissionsBoundaryArn", value=self.permissions_boundary.managed_policy_arn)
        CfnOutput(self, "LogsKeyArn", value=self.logs_key.key_arn)
        CfnOutput(self, "BucketsKeyArn", value=self.buckets_key.key_arn)
        CfnOutput(self, "EbsKeyArn", value=self.ebs_key.key_arn)
        CfnOutput(self, "SecretsKeyArn", value=self.secrets_key.key_arn)
        CfnOutput(self, "SnsKeyArn", value=self.sns_key.key_arn)
        CfnOutput(self, "S3AccessLogsBucketName", value=self.s3_access_logs_bucket.bucket_name)

        NagSuppressions.add_resource_suppressions(
            self.s3_access_logs_bucket,
            [NagPackSuppression(
                id="AwsSolutions-S1",
                reason="This bucket IS the project-wide S3 server-access-log destination that every "
                       "other bucket points to -- a bucket cannot log to itself.",
            )],
        )

        # ------------------------------------------------------------------
        # cdk-nag suppressions (SPEC.md Section 10: "document any
        # suppressions").
        # ------------------------------------------------------------------
        NagSuppressions.add_stack_suppressions(self, [
            NagPackSuppression(
                id="AwsSolutions-IAM4",
                reason="AmazonSSMManagedInstanceCore on SharedInstanceRole is the same AWS-curated, "
                       "SSM-only policy already justified project-wide (see network_stack.py's stack "
                       "suppression) -- unchanged reasoning, now also capped by a permissions boundary.",
            ),
            NagPackSuppression(
                id="AwsSolutions-IAM5",
                reason="The permissions boundary's Allow statement is intentionally broad-but-capped: "
                       "it's a ceiling applied on top of each role's own (narrower) identity policy, not "
                       "a grant by itself -- a role still needs its own policy to actually get any of "
                       "these actions. Wildcard resources on ec2:Describe*/ssm:*/logs:*/ecs:*/vpc-lattice:* "
                       "match this project's existing project-wide IAM5 suppression reasoning (AWS APIs "
                       "with no meaningful resource-level scoping, or CDK-generated read-only Describe "
                       "calls); dynamodb/s3/kms actions above are the only ones that touch real data and "
                       "are either resource-scoped (kms:* to the five CMK ARNs) or already constrained by "
                       "the role's own identity policy for resource selection.",
            ),
        ])

    def _make_key(self, construct_id: str, alias_suffix: str, service_principal: str, description: str) -> kms.Key:
        key = kms.Key(
            self, construct_id,
            alias=f"alias/lattice-lab-{alias_suffix}",
            description=description,
            enable_key_rotation=True,
            removal_policy=RemovalPolicy.DESTROY,
        )
        key.add_to_resource_policy(iam.PolicyStatement(
            sid=f"Allow{service_principal.split('.')[0].capitalize()}ServiceUsage",
            effect=iam.Effect.ALLOW,
            principals=[iam.ServicePrincipal(service_principal)],
            actions=["kms:Decrypt", "kms:DescribeKey", "kms:GenerateDataKey*", "kms:Encrypt", "kms:ReEncrypt*", "kms:CreateGrant"],
            resources=["*"],
            conditions={"Bool": {"kms:GrantIsForAWSResource": "true"}} if service_principal == "ec2.amazonaws.com" else None,
        ))
        return key
