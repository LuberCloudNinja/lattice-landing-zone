"""sagemaker_stack.py -- SageMaker anomaly-detection layer. Gated behind
config.ENABLE_SAGEMAKER (default off; see README's cost warning -- an
Async Inference endpoint bills for its underlying instance while it
exists, scale-to-zero notwithstanding for the instance itself while idle
requests queue).

Async Inference (not Serverless) is the deliberate choice here, per project
direction: Serverless Inference cannot attach to a VPC at all (no
vpc_config on that endpoint type), which would mean scoring live VPC Flow
Log data from a Lambda/endpoint sitting entirely outside app-vpc's network
boundary -- a real break in this project's zero-trust posture. Async
Inference supports VpcConfig AND supports scaling its instance count to
zero via Application Auto Scaling when idle (see rcf_model_promoter's
_register_autoscaling), which together make it the only inference option
here that's both VPC-attached and cost-bounded at rest.

Pipeline (all real, boto3-driven -- there is no `AWS::SageMaker::TrainingJob`
or `...::TransformJob` CloudFormation resource type; training/transform
jobs are inherently one-shot API calls, not declarative resources):
  1. ec2.FlowLog on app-vpc -> S3 (raw/).
  2. flow_log_preprocessor (daily) -- raw/ -> numeric-feature CSV in processed/.
  3. rcf_trainer (weekly) -- CreateTrainingJob (built-in RCF algorithm) on processed/.
  4. rcf_model_promoter (event-driven, on training completion) -- creates the
     new model generation, creates the endpoint on the FIRST successful run
     or updates it on every run after, registers scale-to-zero autoscaling
     on first creation, deletes the previous generation's model/config.
  5. rcf_batch_scorer (every 6h) -- CreateTransformJob against processed/
     using the currently-promoted model (a systematic bulk sweep, distinct
     from the endpoint's on-demand path).
  6. rcf_findings_processor (event-driven, on transform completion) -- reads
     the RCF anomaly scores, writes qualifying findings to DynamoDB + S3,
     publishes to SNS. This is what agentic_ai_stack.py's detect_anomalies
     MCP tool reads (ANOMALY_FINDINGS_TABLE).

Because CreateModel/CreateEndpointConfig/CreateEndpoint/RegisterScalableTarget
for this pipeline happen at Lambda runtime (not synth time -- there's no
model artifact to point at until a real training run produces one), none of
those resources are CDK-managed. teardown_cleanup (wired to `cdk destroy`
via a Custom Resource) sweeps up every NAME_PREFIX-matching endpoint/
model/config/autoscaling-target on stack deletion so nothing this pipeline
creates at runtime survives a teardown -- see that handler's docstring.
"""

from pathlib import Path

from aws_cdk import CfnOutput, CustomResource, Duration, RemovalPolicy, Stack, Tags
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_events as events
from aws_cdk import aws_events_targets as targets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_sns as sns
from aws_cdk import aws_sns_subscriptions as subscriptions
from aws_cdk import custom_resources as cr
from cdk_nag import NagPackSuppression, NagSuppressions
from constructs import Construct

import config
from stacks.network_stack import NetworkStack
from stacks.security_stack import SecurityStack

ASSETS_DIR = Path(__file__).parent / "assets" / "sagemaker"
NAME_PREFIX = "rcf-flowlog-"
ENDPOINT_NAME = f"{NAME_PREFIX}endpoint"
# Built-in Random Cut Forest algorithm container -- AWS-owned first-party
# image; 382416733822 is the documented SageMaker built-in-algorithm
# registry account (verified via web search against AWS's own ECR-paths
# docs). Registry path is region-specific -- built from self.region in the
# constructor below, not hardcoded to one region here.
RCF_TRAINING_IMAGE_ACCOUNT = "382416733822"


class SageMakerStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, *, network: NetworkStack, security: SecurityStack, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)
        Tags.of(self).add("Layer", config.Layer.SAGEMAKER)

        vpc = network.app_vpc
        private_subnets = ec2.SubnetSelection(subnet_group_name="Private")
        subnet_ids = vpc.select_subnets(subnet_group_name="Private").subnet_ids
        rcf_image_uri = f"{RCF_TRAINING_IMAGE_ACCOUNT}.dkr.ecr.{self.region}.amazonaws.com/randomcutforest:latest"

        # ------------------------------------------------------------------
        # VPC endpoints -- sagemaker.api (control plane: CreateTrainingJob
        # etc, called from the Lambdas below) + sagemaker.runtime (data
        # plane: InvokeEndpointAsync). Same "verify, don't assume the
        # NAT-via-inspection path" reasoning as every other interface
        # endpoint in this project.
        # ------------------------------------------------------------------
        sm_endpoint_sg = ec2.SecurityGroup(
            self, "SageMakerVpcEndpointSg",
            vpc=vpc, description="Interface endpoints (sagemaker.api/runtime) for the anomaly-detection Lambdas + endpoint",
            allow_all_outbound=True,
        )
        sm_endpoint_sg.add_ingress_rule(ec2.Peer.ipv4(vpc.vpc_cidr_block), ec2.Port.tcp(443), "app-vpc")
        # Sequential, not parallel -- same Route53 private-hosted-zone
        # provisioning race confirmed live for agentic_ai_stack.py's paired
        # bedrock-runtime/bedrock-agentcore endpoints (see that stack's
        # comment); applied here preventively for the same *.amazonaws.com
        # private-DNS pattern.
        previous_endpoint = None
        for service_id, service in {
            "SagemakerApi": ec2.InterfaceVpcEndpointAwsService.SAGEMAKER_API,
            "SagemakerRuntime": ec2.InterfaceVpcEndpointAwsService.SAGEMAKER_RUNTIME,
        }.items():
            endpoint = ec2.InterfaceVpcEndpoint(
                self, f"{service_id}Endpoint", vpc=vpc, service=service, subnets=private_subnets, security_groups=[sm_endpoint_sg],
            )
            if previous_endpoint is not None:
                endpoint.node.add_dependency(previous_endpoint)
            previous_endpoint = endpoint

        # ------------------------------------------------------------------
        # Data bucket -- one bucket, prefix-partitioned (raw/, processed/,
        # training-output/, transform-output/, findings/) rather than 4-5
        # separate buckets -- fewer resources to remember at teardown time.
        # ------------------------------------------------------------------
        self.data_bucket = s3.Bucket(
            self, "SageMakerDataBucket",
            versioned=True,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.KMS,
            encryption_key=security.buckets_key,
            bucket_key_enabled=True,
            enforce_ssl=True,
            server_access_logs_bucket=security.s3_access_logs_bucket,
            server_access_logs_prefix="sagemaker-data/",
            lifecycle_rules=[s3.LifecycleRule(prefix="raw/", expiration=Duration.days(30))],
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
        )
        ec2.FlowLog(
            self, "AppVpcFlowLog",
            resource_type=ec2.FlowLogResourceType.from_vpc(vpc),
            destination=ec2.FlowLogDestination.to_s3(self.data_bucket, "raw/"),
        )

        # ------------------------------------------------------------------
        # Anomaly findings -- DynamoDB (read by agentic_ai_stack.py's
        # detect_anomalies MCP tool) + SNS (email alert).
        # ------------------------------------------------------------------
        self.anomaly_findings_table = dynamodb.TableV2(
            self, "AnomalyFindingsTable",
            table_name="sagemaker-anomaly-findings",
            partition_key=dynamodb.Attribute(name="finding_type", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="detected_at", type=dynamodb.AttributeType.NUMBER),
            billing=dynamodb.Billing.on_demand(),
            encryption=dynamodb.TableEncryptionV2.customer_managed_key(security.secrets_key),
            point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(point_in_time_recovery_enabled=True),
            removal_policy=RemovalPolicy.DESTROY,
        )

        self.findings_topic = sns.Topic(self, "AnomalyFindingsTopic", topic_name="sagemaker-anomaly-findings", master_key=security.sns_key)
        self.findings_topic.add_subscription(subscriptions.EmailSubscription(config.REMEDIATION_EMAIL))
        async_success_topic = sns.Topic(self, "AsyncInferenceSuccessTopic", topic_name="rcf-async-inference-success", master_key=security.sns_key)
        async_error_topic = sns.Topic(self, "AsyncInferenceErrorTopic", topic_name="rcf-async-inference-error", master_key=security.sns_key)
        async_error_topic.add_subscription(subscriptions.EmailSubscription(config.REMEDIATION_EMAIL))

        # ------------------------------------------------------------------
        # SageMaker execution role -- assumed by training/transform jobs and
        # the model/endpoint containers themselves (NOT the orchestrating
        # Lambdas below, which get their own least-privilege roles).
        # ------------------------------------------------------------------
        sagemaker_execution_role = iam.Role(
            self, "SageMakerExecutionRole",
            assumed_by=iam.ServicePrincipal("sagemaker.amazonaws.com"),
            permissions_boundary=security.permissions_boundary,
        )
        self.data_bucket.grant_read_write(sagemaker_execution_role)
        sagemaker_execution_role.add_to_policy(iam.PolicyStatement(
            sid="VpcAttachedJob",
            actions=["ec2:CreateNetworkInterface", "ec2:DeleteNetworkInterface", "ec2:DescribeNetworkInterfaces",
                     "ec2:DescribeVpcs", "ec2:DescribeSubnets", "ec2:DescribeSecurityGroups", "ec2:DescribeDhcpOptions"],
            resources=["*"],
        ))
        sagemaker_execution_role.add_to_policy(iam.PolicyStatement(
            sid="Logging",
            actions=["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
            resources=[f"arn:aws:logs:{self.region}:{self.account}:log-group:/aws/sagemaker/*"],
        ))

        # ------------------------------------------------------------------
        # Orchestrator Lambdas.
        # ------------------------------------------------------------------
        lambda_sg = ec2.SecurityGroup(self, "SageMakerLambdaSg", vpc=vpc, description="Anomaly-detection orchestrator Lambdas -- outbound only", allow_all_outbound=True)
        common_env = {
            "FLOW_LOGS_BUCKET": self.data_bucket.bucket_name,
            "MODEL_BUCKET": self.data_bucket.bucket_name,
            "RCF_TRAINING_IMAGE": rcf_image_uri,
            "SAGEMAKER_EXECUTION_ROLE_ARN": sagemaker_execution_role.role_arn,
            "SAGEMAKER_SECURITY_GROUP_ID": sm_endpoint_sg.security_group_id,
            "SAGEMAKER_SUBNET_IDS": ",".join(subnet_ids),
            "ENDPOINT_NAME": ENDPOINT_NAME,
        }

        def _orchestrator_role(construct_id: str) -> iam.Role:
            role = iam.Role(
                self, construct_id,
                assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
                permissions_boundary=security.permissions_boundary,
                managed_policies=[iam.ManagedPolicy.from_aws_managed_policy_name("service-role/AWSLambdaVPCAccessExecutionRole")],
            )
            role.add_to_policy(iam.PolicyStatement(
                sid="PassSageMakerExecutionRole", actions=["iam:PassRole"], resources=[sagemaker_execution_role.role_arn],
                conditions={"StringEquals": {"iam:PassedToService": "sagemaker.amazonaws.com"}},
            ))
            return role

        def _make_lambda(name: str, asset_dir: str, role: iam.Role, env: dict, description: str, timeout_seconds: int = 60) -> lambda_.Function:
            log_group = logs.LogGroup(self, f"{name}LogGroup", retention=logs.RetentionDays.ONE_MONTH, encryption_key=security.logs_key, removal_policy=RemovalPolicy.DESTROY)
            return lambda_.Function(
                self, name,
                function_name=f"{NAME_PREFIX}{asset_dir.replace('_', '-')}",
                description=description,
                runtime=lambda_.Runtime.PYTHON_3_13,
                architecture=lambda_.Architecture.ARM_64,
                handler="handler.handler",
                code=lambda_.Code.from_asset(str(ASSETS_DIR / asset_dir)),
                timeout=Duration.seconds(timeout_seconds),
                role=role,
                vpc=vpc, vpc_subnets=private_subnets, security_groups=[lambda_sg],
                log_group=log_group,
                environment=env,
            )

        preprocessor_role = _orchestrator_role("FlowLogPreprocessorRole")
        self.data_bucket.grant_read_write(preprocessor_role)
        preprocessor_fn = _make_lambda("FlowLogPreprocessor", "flow_log_preprocessor", preprocessor_role, {
            "FLOW_LOGS_BUCKET": self.data_bucket.bucket_name, "RAW_PREFIX": "raw/", "PROCESSED_PREFIX": "processed/",
        }, "Daily: extract numeric features from raw VPC Flow Logs into training-ready CSV.", timeout_seconds=300)

        trainer_role = _orchestrator_role("RcfTrainerRole")
        self.data_bucket.grant_read_write(trainer_role)
        trainer_role.add_to_policy(iam.PolicyStatement(actions=["sagemaker:CreateTrainingJob"], resources=["*"]))
        trainer_fn = _make_lambda("RcfTrainer", "rcf_trainer", trainer_role, {**common_env}, "Weekly: kick off an RCF training job against accumulated processed flow-log data.")

        promoter_role = _orchestrator_role("RcfModelPromoterRole")
        promoter_role.add_to_policy(iam.PolicyStatement(
            actions=["sagemaker:CreateModel", "sagemaker:CreateEndpointConfig", "sagemaker:CreateEndpoint", "sagemaker:UpdateEndpoint",
                     "sagemaker:DescribeEndpoint", "sagemaker:DescribeEndpointConfig", "sagemaker:DeleteModel", "sagemaker:DeleteEndpointConfig"],
            resources=["*"],
        ))
        promoter_role.add_to_policy(iam.PolicyStatement(
            actions=["application-autoscaling:RegisterScalableTarget", "application-autoscaling:PutScalingPolicy"], resources=["*"],
        ))
        promoter_fn = _make_lambda("RcfModelPromoter", "rcf_model_promoter", promoter_role, {
            **common_env,
            "ASYNC_OUTPUT_S3_PATH": f"s3://{self.data_bucket.bucket_name}/async-output/",
            "KMS_KEY_ID": security.buckets_key.key_id,
            "SUCCESS_TOPIC_ARN": async_success_topic.topic_arn,
            "ERROR_TOPIC_ARN": async_error_topic.topic_arn,
            "MAX_CAPACITY": "1",
        }, "Event-driven: promote a newly-trained RCF model onto the Async Inference endpoint.")
        async_success_topic.grant_publish(sagemaker_execution_role)
        async_error_topic.grant_publish(sagemaker_execution_role)

        scorer_role = _orchestrator_role("RcfBatchScorerRole")
        self.data_bucket.grant_read_write(scorer_role)
        scorer_role.add_to_policy(iam.PolicyStatement(actions=["sagemaker:CreateTransformJob", "sagemaker:DescribeEndpoint", "sagemaker:DescribeEndpointConfig"], resources=["*"]))
        scorer_fn = _make_lambda("RcfBatchScorer", "rcf_batch_scorer", scorer_role, {**common_env}, "Every 6h: bulk-score accumulated flow-log batches with the currently-promoted model.")

        findings_role = _orchestrator_role("RcfFindingsProcessorRole")
        self.data_bucket.grant_read(findings_role)
        self.anomaly_findings_table.grant_write_data(findings_role)
        self.findings_topic.grant_publish(findings_role)
        findings_fn = _make_lambda("RcfFindingsProcessor", "rcf_findings_processor", findings_role, {
            "ANOMALY_FINDINGS_TABLE": self.anomaly_findings_table.table_name,
            "FINDINGS_BUCKET": self.data_bucket.bucket_name,
            "TOPIC_ARN": self.findings_topic.topic_arn,
            "ANOMALY_SCORE_THRESHOLD": "3.0",
        }, "Event-driven: turn qualifying RCF anomaly scores into findings (DynamoDB + S3 + SNS).")

        # ------------------------------------------------------------------
        # Schedules + event-driven triggers.
        # ------------------------------------------------------------------
        events.Rule(self, "FlowLogPreprocessorSchedule", schedule=events.Schedule.rate(Duration.days(1)), targets=[targets.LambdaFunction(preprocessor_fn)])
        events.Rule(self, "RcfTrainerSchedule", schedule=events.Schedule.rate(Duration.days(7)), targets=[targets.LambdaFunction(trainer_fn)])
        events.Rule(self, "RcfBatchScorerSchedule", schedule=events.Schedule.rate(Duration.hours(6)), targets=[targets.LambdaFunction(scorer_fn)])

        events.Rule(
            self, "TrainingJobCompletionRule",
            event_pattern=events.EventPattern(source=["aws.sagemaker"], detail_type=["SageMaker Training Job State Change"], detail={"TrainingJobName": [{"prefix": "rcf-flowlog-training-"}]}),
            targets=[targets.LambdaFunction(promoter_fn)],
        )
        events.Rule(
            self, "TransformJobCompletionRule",
            event_pattern=events.EventPattern(source=["aws.sagemaker"], detail_type=["SageMaker Transform Job State Change"], detail={"TransformJobName": [{"prefix": "rcf-batch-score-"}]}),
            targets=[targets.LambdaFunction(findings_fn)],
        )

        # ------------------------------------------------------------------
        # Teardown sweep -- see teardown_cleanup/handler.py's docstring.
        # ------------------------------------------------------------------
        cleanup_role = iam.Role(
            self, "TeardownCleanupRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            permissions_boundary=security.permissions_boundary,
            managed_policies=[iam.ManagedPolicy.from_aws_managed_policy_name("service-role/AWSLambdaBasicExecutionRole")],
        )
        cleanup_role.add_to_policy(iam.PolicyStatement(
            actions=["sagemaker:ListEndpoints", "sagemaker:DeleteEndpoint", "sagemaker:ListEndpointConfigs", "sagemaker:DeleteEndpointConfig",
                     "sagemaker:ListModels", "sagemaker:DeleteModel", "application-autoscaling:DeregisterScalableTarget"],
            resources=["*"],
        ))
        cleanup_fn = lambda_.Function(
            self, "TeardownCleanup",
            description="On stack delete only: sweeps any Lambda-runtime-created RCF endpoint/model/config/autoscaling-target.",
            runtime=lambda_.Runtime.PYTHON_3_13,
            architecture=lambda_.Architecture.ARM_64,
            handler="handler.handler",
            code=lambda_.Code.from_asset(str(ASSETS_DIR / "teardown_cleanup")),
            timeout=Duration.seconds(120),
            role=cleanup_role,
            environment={"NAME_PREFIX": NAME_PREFIX},
        )
        cleanup_provider = cr.Provider(self, "TeardownCleanupProvider", on_event_handler=cleanup_fn)
        CustomResource(self, "TeardownCleanupResource", service_token=cleanup_provider.service_token)

        # ------------------------------------------------------------------
        # Outputs
        # ------------------------------------------------------------------
        CfnOutput(self, "SageMakerDataBucketName", value=self.data_bucket.bucket_name)
        CfnOutput(self, "AnomalyFindingsTableName", value=self.anomaly_findings_table.table_name)
        CfnOutput(self, "AnomalyFindingsTopicArn", value=self.findings_topic.topic_arn)
        CfnOutput(self, "RcfEndpointName", value=ENDPOINT_NAME)

        # ------------------------------------------------------------------
        # cdk-nag suppressions (SPEC.md Section 10).
        # ------------------------------------------------------------------
        NagSuppressions.add_stack_suppressions(self, [
            NagPackSuppression(
                id="AwsSolutions-IAM4",
                reason="service-role/AWSLambdaVPCAccessExecutionRole and .../AWSLambdaBasicExecutionRole are the "
                       "AWS-curated policies required for their respective Lambda capabilities (ENI management for "
                       "VPC attachment; basic CloudWatch Logs access) -- no narrower alternative exists.",
            ),
            NagPackSuppression(
                id="AwsSolutions-IAM5",
                reason="sagemaker:*/application-autoscaling:* actions use resource='*' because CreateTrainingJob/"
                       "CreateModel/CreateEndpoint etc name their own resources at call time (new generation names "
                       "each promotion) -- there's no ARN to scope to before the call creates it. The real scoping "
                       "control is the explicit action allow-list (no wildcard actions) and the NAME_PREFIX "
                       "convention every resource this pipeline creates follows.",
            ),
            NagPackSuppression(
                id="AwsSolutions-L1",
                reason="PYTHON_3_13 is the latest available managed Lambda runtime as of this build.",
            ),
            NagPackSuppression(
                id="AwsSolutions-S1",
                reason="SageMakerDataBucket has server_access_logs_bucket set (security.s3_access_logs_bucket) -- "
                       "this fires on that access-LOGS bucket itself, which this project (see security_stack.py) "
                       "deliberately does not recursively log to avoid an infinite logging loop.",
            ),
        ])
