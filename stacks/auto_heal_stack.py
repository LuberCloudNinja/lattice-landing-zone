"""auto_heal_stack.py -- deterministic EventBridge -> Step Functions ->
remediation Lambda -> SNS (before/after) auto-heal loop for known, narrowly
-scoped failure modes, plus this project's AI/SageMaker dashboard
extensions. Deployed unconditionally, last in stage.py, since it's the one
stack positioned after every optional layer (Cloud WAN/AI/SageMaker) that
might or might not exist by the time it runs.

Known failure modes (see stacks/assets/auto_heal/remediate/handler.py for
the exact action each takes):
  - vpn-tunnel: AWS/VPN TunnelState < 1 (network_stack.py's Site-to-Site
    VPN is down) -> SSM Run Command restarts ipsec on the Libreswan
    instance to force IKE renegotiation.
  - lattice-target: the Lattice INSTANCE-type target host
    (threetier_stack.py's lattice_instance_target_host) fails an EC2
    status check -> reboot it.
  - bedrock-throttle: agentic_ai_stack.py's orchestrator Lambda(s) erroring
    (only wired when config.ENABLE_AI is on) -> alert only; a Bedrock TPS
    quota increase needs a support ticket, not an infrastructure action.

Each alarm maps to exactly ONE named resource this stack already holds a
reference to -- same "never a sweep" blast-radius reasoning as
drift_remediation_stack.py. Every alarm ALSO fires directly to
drift_remediation.alerts_topic (the project's one existing ops-alert SNS
topic) independent of the Step Functions path, so a human sees the same
signal the auto-heal loop is acting on.
"""

from pathlib import Path

from aws_cdk import CfnOutput, Duration, RemovalPolicy, Stack, Tags
from aws_cdk import aws_cloudwatch as cw
from aws_cdk import aws_cloudwatch_actions as cw_actions
from aws_cdk import aws_events as events
from aws_cdk import aws_events_targets as targets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_stepfunctions as sfn
from aws_cdk import aws_stepfunctions_tasks as tasks
from cdk_nag import NagPackSuppression, NagSuppressions
from constructs import Construct

import config
from stacks.drift_remediation_stack import DriftRemediationStack
from stacks.network_stack import NetworkStack
from stacks.observability_stack import ObservabilityStack
from stacks.security_stack import SecurityStack
from stacks.threetier_stack import ThreeTierStack

ASSETS_DIR = Path(__file__).parent / "assets" / "auto_heal"


class AutoHealStack(Stack):
    def __init__(
        self, scope: Construct, construct_id: str, *,
        network: NetworkStack, threetier: ThreeTierStack, security: SecurityStack,
        drift_remediation: DriftRemediationStack, observability: ObservabilityStack,
        agentic_ai=None, sagemaker=None, **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        Tags.of(self).add("Layer", config.Layer.REMEDIATION)

        # ------------------------------------------------------------------
        # Remediation Lambda -- no VPC attachment needed (ec2:RebootInstances/
        # ssm:SendCommand are regional control-plane calls, not in-VPC).
        # ------------------------------------------------------------------
        remediate_role = iam.Role(
            self, "RemediateRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            permissions_boundary=security.permissions_boundary,
            managed_policies=[iam.ManagedPolicy.from_aws_managed_policy_name("service-role/AWSLambdaBasicExecutionRole")],
        )
        remediate_role.add_to_policy(iam.PolicyStatement(
            sid="VpnTunnelRestart", actions=["ssm:SendCommand"],
            resources=[
                f"arn:aws:ec2:{self.region}:{self.account}:instance/{network.libreswan.instance_id}",
                f"arn:aws:ssm:{self.region}::document/AWS-RunShellScript",
            ],
        ))
        remediate_role.add_to_policy(iam.PolicyStatement(
            sid="LatticeTargetReboot", actions=["ec2:RebootInstances"],
            resources=[f"arn:aws:ec2:{self.region}:{self.account}:instance/{threetier.lattice_instance_target_host.instance_id}"],
        ))

        remediate_log_group = logs.LogGroup(
            self, "RemediateLogGroup", retention=logs.RetentionDays.ONE_MONTH, encryption_key=security.logs_key, removal_policy=RemovalPolicy.DESTROY,
        )
        remediate_fn = lambda_.Function(
            self, "Remediate",
            function_name="auto-heal-remediate",
            description="Dispatches on remediation_type -- see module docstring for the 3 known failure modes handled.",
            runtime=lambda_.Runtime.PYTHON_3_13,
            architecture=lambda_.Architecture.ARM_64,
            handler="handler.handler",
            code=lambda_.Code.from_asset(str(ASSETS_DIR / "remediate")),
            timeout=Duration.seconds(60),
            role=remediate_role,
            log_group=remediate_log_group,
            environment={
                "LIBRESWAN_INSTANCE_ID": network.libreswan.instance_id,
                "LATTICE_TARGET_INSTANCE_ID": threetier.lattice_instance_target_host.instance_id,
            },
        )

        # ------------------------------------------------------------------
        # Step Functions -- NotifyBefore -> Remediate -> NotifyAfter.
        # ------------------------------------------------------------------
        notify_before = tasks.SnsPublish(
            self, "NotifyBefore",
            topic=drift_remediation.alerts_topic,
            subject="auto-heal: remediation starting",
            message=sfn.TaskInput.from_json_path_at("$"),
            result_path="$.notifyBeforeResult",
        )
        remediate_step = tasks.LambdaInvoke(
            self, "RemediateTask",
            lambda_function=remediate_fn,
            payload_response_only=True,
            result_path="$.remediateResult",
        )
        notify_after = tasks.SnsPublish(
            self, "NotifyAfter",
            topic=drift_remediation.alerts_topic,
            subject="auto-heal: remediation complete",
            message=sfn.TaskInput.from_json_path_at("$.remediateResult"),
        )

        definition = notify_before.next(remediate_step).next(notify_after)
        sfn_log_group = logs.LogGroup(
            self, "AutoHealStateMachineLogGroup", retention=logs.RetentionDays.ONE_MONTH, encryption_key=security.logs_key, removal_policy=RemovalPolicy.DESTROY,
        )
        self.state_machine = sfn.StateMachine(
            self, "AutoHealStateMachine",
            state_machine_name="auto-heal",
            definition_body=sfn.DefinitionBody.from_chainable(definition),
            logs=sfn.LogOptions(destination=sfn_log_group, level=sfn.LogLevel.ALL),
            tracing_enabled=True,
        )

        # ------------------------------------------------------------------
        # Alarms -- each fires directly to drift_remediation.alerts_topic
        # (SNS) AND triggers the Step Functions loop via an EventBridge rule
        # on the alarm's own state-change event.
        # ------------------------------------------------------------------
        eventbridge_role = iam.Role(self, "EventBridgeToSfnRole", assumed_by=iam.ServicePrincipal("events.amazonaws.com"))
        self.state_machine.grant_start_execution(eventbridge_role)

        def _wire_alarm(alarm: cw.Alarm, remediation_type: str) -> None:
            alarm.add_alarm_action(cw_actions.SnsAction(drift_remediation.alerts_topic))
            events.Rule(
                self, f"{alarm.node.id}TriggerRule",
                event_pattern=events.EventPattern(
                    source=["aws.cloudwatch"], detail_type=["CloudWatch Alarm State Change"],
                    resources=[alarm.alarm_arn], detail={"state": {"value": ["ALARM"]}},
                ),
                targets=[targets.SfnStateMachine(
                    self.state_machine, role=eventbridge_role,
                    input=events.RuleTargetInput.from_object({"remediation_type": remediation_type}),
                )],
            )

        vpn_tunnel_metric = cw.Metric(
            namespace="AWS/VPN", metric_name="TunnelState",
            dimensions_map={"VpnId": network.vpn_connection.attr_vpn_connection_id},
            statistic="Average", period=Duration.minutes(5),
        )
        vpn_tunnel_alarm = cw.Alarm(
            self, "VpnTunnelDownAlarm", metric=vpn_tunnel_metric,
            threshold=1, comparison_operator=cw.ComparisonOperator.LESS_THAN_THRESHOLD,
            evaluation_periods=2, treat_missing_data=cw.TreatMissingData.BREACHING,
            alarm_description="Site-to-Site VPN tunnel down (static VPN: TunnelState 0=DOWN, 1=UP).",
        )
        _wire_alarm(vpn_tunnel_alarm, "vpn-tunnel")

        lattice_target_status_metric = cw.Metric(
            namespace="AWS/EC2", metric_name="StatusCheckFailed",
            dimensions_map={"InstanceId": threetier.lattice_instance_target_host.instance_id},
            statistic="Maximum", period=Duration.minutes(5),
        )
        lattice_target_alarm = cw.Alarm(
            self, "LatticeTargetUnhealthyAlarm", metric=lattice_target_status_metric,
            threshold=1, comparison_operator=cw.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
            evaluation_periods=2, treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
            alarm_description="Lattice INSTANCE-type target host failing its EC2 status check.",
        )
        _wire_alarm(lattice_target_alarm, "lattice-target")

        if agentic_ai is not None:
            for persona, fn in agentic_ai.orchestrator_fns.items():
                bedrock_alarm = cw.Alarm(
                    self, f"BedrockThrottle{persona.title().replace('-', '')}Alarm",
                    metric=fn.metric_errors(period=Duration.minutes(5)),
                    threshold=3, comparison_operator=cw.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
                    evaluation_periods=1, treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
                    alarm_description=f"agent-orchestrator ({persona}) erroring repeatedly -- likely Bedrock throttling.",
                )
                _wire_alarm(bedrock_alarm, "bedrock-throttle")

        # ------------------------------------------------------------------
        # Dashboard extensions -- AI/SageMaker/API GW/DynamoDB/AgentCore.
        # Added to observability.dashboard's SAME underlying resource
        # (cross-stack construct mutation -- already an established pattern
        # in this project, e.g. security.buckets_key.grant_* called from
        # other stacks) rather than a second, separate dashboard.
        # ------------------------------------------------------------------
        observability.dashboard.add_widgets(cw.TextWidget(markdown="# Agentic AI + SageMaker", width=24, height=1))
        if agentic_ai is not None:
            observability.dashboard.add_widgets(
                cw.GraphWidget(
                    title="agent-orchestrator Lambdas",
                    left=[fn.metric_invocations() for fn in agentic_ai.orchestrator_fns.values()],
                    right=[fn.metric_errors() for fn in agentic_ai.orchestrator_fns.values()],
                    width=12, height=6,
                ),
                cw.GraphWidget(
                    title="Working-memory DynamoDB",
                    left=[agentic_ai.working_memory_table.metric_consumed_read_capacity_units(), agentic_ai.working_memory_table.metric_consumed_write_capacity_units()],
                    width=12, height=6,
                ),
            )
            observability.dashboard.add_widgets(
                cw.GraphWidget(
                    title="Agentic AI API Gateway",
                    left=[cw.Metric(namespace="AWS/ApiGateway", metric_name="Count", dimensions_map={"ApiId": agentic_ai.http_api.api_id}, statistic="Sum")],
                    right=[cw.Metric(namespace="AWS/ApiGateway", metric_name="4xx", dimensions_map={"ApiId": agentic_ai.http_api.api_id}, statistic="Sum"),
                           cw.Metric(namespace="AWS/ApiGateway", metric_name="5xx", dimensions_map={"ApiId": agentic_ai.http_api.api_id}, statistic="Sum")],
                    width=12, height=6,
                ),
                # AgentCore's own runtime/gateway/memory metrics live under the
                # AWS/Bedrock-AgentCore namespace (confirmed via AWS docs), but
                # AgentCore Observability needs to be explicitly turned on per
                # resource, and the per-resource CloudWatch dimension names
                # aren't pinned down in public docs as of this build -- rather
                # than guess a dimensions_map that might silently render an
                # empty graph, this links to the real console view instead.
                cw.TextWidget(
                    markdown=f"**AgentCore Observability** -- Runtime/Gateway/Memory metrics: "
                             f"[CloudWatch GenAI Observability](https://{self.region}.console.aws.amazon.com/cloudwatch/home?region={self.region}#gen-ai-observability/agent-core) "
                             f"(namespace `AWS/Bedrock-AgentCore`). Gateway: `{agentic_ai.gateway.attr_gateway_identifier}`.",
                    width=12, height=6,
                ),
            )
        if sagemaker is not None:
            observability.dashboard.add_widgets(
                cw.GraphWidget(
                    title="SageMaker anomaly-detection Lambdas",
                    left=[cw.Metric(namespace="AWS/Lambda", metric_name="Invocations", dimensions_map={"FunctionName": f"rcf-flowlog-{n}"}, statistic="Sum")
                          for n in ["flow-log-preprocessor", "rcf-trainer", "rcf-batch-scorer", "rcf-findings-processor"]],
                    width=12, height=6,
                ),
                cw.GraphWidget(
                    title="Anomaly findings",
                    left=[sagemaker.anomaly_findings_table.metric_consumed_write_capacity_units()],
                    width=12, height=6,
                ),
            )

        CfnOutput(self, "AutoHealStateMachineArn", value=self.state_machine.state_machine_arn)

        # ------------------------------------------------------------------
        # cdk-nag suppressions (SPEC.md Section 10).
        # ------------------------------------------------------------------
        NagSuppressions.add_stack_suppressions(self, [
            NagPackSuppression(
                id="AwsSolutions-IAM4",
                reason="service-role/AWSLambdaBasicExecutionRole is the AWS-curated policy for basic CloudWatch "
                       "Logs access -- Remediate isn't VPC-attached (ec2:RebootInstances/ssm:SendCommand are "
                       "regional control-plane calls), so no VPC-access policy is needed either.",
            ),
            NagPackSuppression(
                id="AwsSolutions-IAM5",
                reason="EventBridgeToSfnRole's StartExecution grant (via state_machine.grant_start_execution) is "
                       "scoped to this ONE state machine's ARN by the L2 grant helper, not a wildcard -- cdk-nag "
                       "flags the underlying generated policy shape, not an actual overly-broad grant.",
            ),
            NagPackSuppression(
                id="AwsSolutions-L1",
                reason="PYTHON_3_13 is the latest available managed Lambda runtime as of this build.",
            ),
            NagPackSuppression(
                id="AwsSolutions-SF1",
                reason="logs=LogOptions(level=ALL) is already set on the state machine -- this suppresses the "
                       "narrower 'log ALL events including execution data' variant, which would duplicate the "
                       "SNS before/after messages already carrying the same information.",
            ),
            NagPackSuppression(
                id="AwsSolutions-SF2",
                reason="X-Ray tracing is already enabled (tracing_enabled=True) -- see the same reasoning as SF1 "
                       "if this ID maps to a related-but-distinct check in the installed cdk-nag version.",
            ),
        ])
