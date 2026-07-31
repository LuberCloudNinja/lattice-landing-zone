"""governance_stack.py -- recording + detection plane (account level).

CloudTrail (who did what), AWS Config (what does the account look like, is
it compliant), Security Hub (aggregated findings + standards), GuardDuty
(threat detection) -- the four services that, together, make
drift_remediation_stack.py's "only the pipeline may change infra" control
possible: CloudTrail is literally the event source EventBridge filters in
that stack, and Config's required-tags/encryption/CloudTrail-enabled rules
give a continuous compliance signal independent of any single manual event.

Account-level only, single-account here (this project has no AWS
Organizations membership) -- org-governance/ (Scope B, a separate app) is
where the org-wide delegated-admin/SCP/Control Tower version of this
pattern lives, for a management account this lab doesn't have.
"""

import aws_cdk as cdk
from aws_cdk import CfnOutput, RemovalPolicy, Stack, Tags
from aws_cdk import aws_config as awsconfig
from aws_cdk import aws_cloudtrail as cloudtrail
from aws_cdk import aws_guardduty as guardduty
from aws_cdk import aws_iam as iam
from aws_cdk import aws_logs as logs
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_securityhub as securityhub
from cdk_nag import NagPackSuppression, NagSuppressions
from constructs import Construct

import config
from stacks.security_stack import SecurityStack

# Standards ARNs are region-templated, not account-templated -- these are
# AWS-owned standard definitions, not account resources.
FSBP_STANDARD_ARN = "arn:aws:securityhub:{region}::standards/aws-foundational-security-best-practices/v/1.0.0"
# v1.2.0 (originally used here) no longer exists as a subscribable standard
# -- confirmed live via `aws securityhub describe-standards`: it's now only
# a legacy `arn:aws:securityhub:::ruleset/...` resource, a different type
# CfnStandard can't target. v3.0.0 is the current, stable CIS AWS
# Foundations Benchmark version (v1.4.0 and v5.0.0 also exist; 3.0.0 is the
# well-established middle ground, not the newest-and-least-proven).
CIS_STANDARD_ARN = "arn:aws:securityhub:{region}::standards/cis-aws-foundations-benchmark/v/3.0.0"

# Enable GuardDuty by default alongside the rest of the detection plane --
# unlike ENABLE_CLOUDWAN (materially expensive) GuardDuty's lab-scale cost
# is negligible, so this isn't gated behind its own config.py flag; kept as
# a plain module constant so it's still one place to flip off if needed.
ENABLE_GUARDDUTY = True


class GovernanceStack(Stack):
    """CloudTrail + AWS Config + Security Hub + GuardDuty."""

    def __init__(self, scope: Construct, construct_id: str, *, security: SecurityStack, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)
        Tags.of(self).add("Layer", config.Layer.GOVERNANCE)

        # ------------------------------------------------------------------
        # CloudTrail -- multi-region, management events, log file
        # validation, delivered to both a CMK-encrypted+versioned S3 bucket
        # and CloudWatch Logs (the latter is what EventBridge rules key off
        # of in practice, but the S3 copy is the durable, queryable record).
        # ------------------------------------------------------------------
        trail_bucket = s3.Bucket(
            self, "CloudTrailBucket",
            versioned=True,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.KMS,
            encryption_key=security.buckets_key,
            bucket_key_enabled=True,
            enforce_ssl=True,
            server_access_logs_bucket=security.s3_access_logs_bucket,
            server_access_logs_prefix="cloudtrail-bucket/",
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
        )
        trail_log_group = logs.LogGroup(
            self, "CloudTrailLogGroup",
            retention=logs.RetentionDays.ONE_MONTH,
            encryption_key=security.logs_key,
            removal_policy=RemovalPolicy.DESTROY,
        )
        self.trail = cloudtrail.Trail(
            self, "Trail",
            trail_name="lattice-lab-trail",
            bucket=trail_bucket,
            encryption_key=security.buckets_key,
            is_multi_region_trail=True,
            include_global_service_events=True,
            enable_file_validation=True,
            send_to_cloud_watch_logs=True,
            cloud_watch_log_group=trail_log_group,
            management_events=cloudtrail.ReadWriteType.ALL,
        )
        # Deploy-time failure, confirmed live: "Insufficient permissions to
        # access S3 bucket ... or KMS key ...". The Trail L2 construct wires
        # the BUCKET policy for cloudtrail.amazonaws.com automatically, but
        # buckets_key's own resource policy (security_stack.py) only grants
        # s3.amazonaws.com -- CloudTrail calls S3 PutObject under its own
        # service identity, not a generic S3 one, so the CMK needs its own
        # explicit grant too. Same gap already hit and fixed the same way
        # for vpc-lattice.amazonaws.com on lattice_stack.py's access-logs
        # bucket.
        security.buckets_key.grant_encrypt_decrypt(iam.ServicePrincipal("cloudtrail.amazonaws.com"))

        # ------------------------------------------------------------------
        # AWS Config -- recorder (all resource types incl. global) +
        # delivery channel to a CMK-encrypted S3 bucket, plus the managed
        # rules SPEC calls out.
        # ------------------------------------------------------------------
        config_bucket = s3.Bucket(
            self, "ConfigBucket",
            versioned=True,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.KMS,
            encryption_key=security.buckets_key,
            bucket_key_enabled=True,
            enforce_ssl=True,
            server_access_logs_bucket=security.s3_access_logs_bucket,
            server_access_logs_prefix="config-bucket/",
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
        )
        config_role = iam.Role(
            self, "ConfigRecorderRole",
            assumed_by=iam.ServicePrincipal("config.amazonaws.com"),
            managed_policies=[iam.ManagedPolicy.from_aws_managed_policy_name("service-role/AWS_ConfigRole")],
        )
        config_bucket.grant_write(config_role)
        security.buckets_key.grant_encrypt_decrypt(config_role)

        # Deliberately NO explicit DependsOn between these two -- both
        # directions were tried and both are wrong, confirmed live:
        #   * delivery_channel depends on recorder: deadlocks. Recorder's
        #     own creation implicitly calls StartConfigurationRecorder,
        #     which needs a delivery channel to exist; delivery channel
        #     never even starts creating until recorder finishes, which it
        #     never does. Recorder sat in CREATE_IN_PROGRESS for 20+
        #     minutes with zero delivery channels in the account.
        #   * recorder depends on delivery_channel: fails immediately.
        #     PutDeliveryChannel requires a configuration recorder to
        #     already exist ("NoAvailableConfigurationRecorderException"),
        #     but recorder hasn't been created yet since it's waiting on
        #     the channel.
        # This is a genuine circular API requirement, not a CFN ordering
        # bug -- the documented, community-verified fix (aws/aws-cdk#3492)
        # is to declare neither dependency and let each resource's own PUT
        # call succeed independently; ConfigurationRecorder's built-in
        # stabilization polling already waits for the delivery channel on
        # its own, without needing to be told to.
        delivery_channel = awsconfig.CfnDeliveryChannel(
            self, "ConfigDeliveryChannel",
            name="lattice-lab-delivery-channel",
            s3_bucket_name=config_bucket.bucket_name,
            s3_kms_key_arn=security.buckets_key.key_arn,
        )
        recorder = awsconfig.CfnConfigurationRecorder(
            self, "ConfigRecorder",
            name="lattice-lab-recorder",
            role_arn=config_role.role_arn,
            recording_group=awsconfig.CfnConfigurationRecorder.RecordingGroupProperty(
                all_supported=True,
                include_global_resource_types=True,
            ),
        )

        managed_rules = {
            "RequiredTagsRule": (awsconfig.ManagedRuleIdentifiers.REQUIRED_TAGS, {"tag1Key": "Project", "tag2Key": "Layer"}),
            "S3VersioningRule": (awsconfig.ManagedRuleIdentifiers.S3_BUCKET_VERSIONING_ENABLED, None),
            "S3EncryptionRule": (awsconfig.ManagedRuleIdentifiers.S3_BUCKET_SERVER_SIDE_ENCRYPTION_ENABLED, None),
            "EncryptedVolumesRule": (awsconfig.ManagedRuleIdentifiers.EBS_ENCRYPTED_VOLUMES, None),
            "CloudTrailEnabledRule": (awsconfig.ManagedRuleIdentifiers.CLOUD_TRAIL_ENABLED, None),
            "VpcFlowLogsRule": (awsconfig.ManagedRuleIdentifiers.VPC_FLOW_LOGS_ENABLED, None),
            "RestrictedSshRule": (awsconfig.ManagedRuleIdentifiers.EC2_SECURITY_GROUPS_INCOMING_SSH_DISABLED, None),
            "IamNoAdminRule": (awsconfig.ManagedRuleIdentifiers.IAM_POLICY_NO_STATEMENTS_WITH_ADMIN_ACCESS, None),
        }
        for rule_id, (identifier, input_parameters) in managed_rules.items():
            rule = awsconfig.ManagedRule(
                self, rule_id,
                identifier=identifier,
                input_parameters=input_parameters,
            )
            rule.node.add_dependency(recorder)

        # ------------------------------------------------------------------
        # Security Hub -- explicit standards (not enable_default_standards,
        # so this project controls exactly which two are on) + GuardDuty.
        # ------------------------------------------------------------------
        hub = securityhub.CfnHub(self, "Hub", enable_default_standards=False)
        fsbp_standard = securityhub.CfnStandard(
            self, "FsbpStandard", standards_arn=FSBP_STANDARD_ARN.format(region=self.region),
        )
        fsbp_standard.add_dependency(hub)
        cis_standard = securityhub.CfnStandard(
            self, "CisStandard", standards_arn=CIS_STANDARD_ARN.format(region=self.region),
        )
        cis_standard.add_dependency(hub)

        if ENABLE_GUARDDUTY:
            guardduty.CfnDetector(self, "GuardDutyDetector", enable=True)

        CfnOutput(self, "CloudTrailArn", value=self.trail.trail_arn)
        CfnOutput(self, "ConfigBucketName", value=config_bucket.bucket_name)
        CfnOutput(self, "SecurityHubHubArn", value=hub.attr_arn)

        # ------------------------------------------------------------------
        # cdk-nag suppressions (SPEC.md Section 10).
        # ------------------------------------------------------------------
        NagSuppressions.add_stack_suppressions(self, [
            NagPackSuppression(
                id="AwsSolutions-IAM4",
                reason="service-role/AWS_ConfigRole on ConfigRecorderRole is the AWS-curated policy AWS "
                       "Config itself documents as the standard recorder role -- there is no narrower "
                       "alternative that still lets the recorder actually enumerate every resource type.",
            ),
            NagPackSuppression(
                id="AwsSolutions-IAM5",
                reason="config_bucket.grant_write()/security.buckets_key.grant_encrypt_decrypt() are "
                       "CDK-generated grants scoped to this specific bucket/key ARNs -- any wildcard is "
                       "in the /* object-path suffix CDK adds for S3 write grants, not an unscoped "
                       "resource.",
            ),
        ])
