"""privatelink_stack.py -- provider NLB + endpoint service + consumer interface endpoint.

See SPEC.md Section 6 ("privatelink_stack.py"). Note: this stack's NLB is a
legitimate, correct use of a plain Network Load Balancer -- it fronts the
PrivateLink provider app, and is unrelated to the GWLB inspection data path
in inspection_stack.py (see docs/inspection-architecture-reference.md
Section 4.0 for why those two must never be conflated).

"One-way and overlapping-CIDR-safe" (SPEC.md Section 6) is demonstrated by
construction, not a separate feature to build: PrivateLink works by
projecting the provider's NLB into the *consumer's* VPC as an ENI (the
Interface Endpoint) -- there is no VPC peering, no route table entry for
provider-vpc's CIDR anywhere in app-vpc, and no route table entry for
app-vpc's CIDR in provider-vpc. The consumer can reach the provider; the
provider has no corresponding path back into the consumer's VPC at all
(one-way), and because there's no routing between the two CIDR spaces, they
could freely overlap without conflict (this project's don't -- 10.1.0.0/16
vs 10.2.0.0/16 -- but nothing here depends on that).
"""

from pathlib import Path

import aws_cdk as cdk
from aws_cdk import CfnOutput, Stack, Tags
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_ecs as ecs
from aws_cdk import aws_elasticloadbalancingv2 as elbv2
from aws_cdk import aws_logs as logs
from cdk_nag import NagPackSuppression, NagSuppressions
from constructs import Construct

import config
from stacks.network_stack import NetworkStack

PROVIDER_PORT = 8080
PROVIDER_ASSETS_DIR = Path(__file__).parent / "assets" / "privatelink"


class PrivateLinkStack(Stack):
    """Provider app behind an NLB, exposed as a VPC endpoint service; app-vpc consumes it."""

    def __init__(self, scope: Construct, construct_id: str, *, network: NetworkStack, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)
        Tags.of(self).add("Layer", config.Layer.PRIVATELINK)

        provider_vpc = network.provider_vpc
        app_vpc = network.app_vpc

        # ------------------------------------------------------------------
        # Provider app: a minimal stdlib HTTP server (no dependencies to
        # install at boot), just enough to prove the PrivateLink path works
        # end to end -- not a real workload.
        # ------------------------------------------------------------------
        provider_sg = ec2.SecurityGroup(
            self, "ProviderInstanceSg",
            vpc=provider_vpc,
            description="Provider app -- inbound from the NLB health checks / traffic only",
            allow_all_outbound=True,
        )
        provider_sg.add_ingress_rule(
            ec2.Peer.ipv4(provider_vpc.vpc_cidr_block), ec2.Port.tcp(PROVIDER_PORT), "provider-vpc (NLB targets by IP, preserves source)"
        )

        # ------------------------------------------------------------------
        # VPC endpoints Fargate needs to even START in provider-vpc.
        # provider-vpc's Private subnets are PRIVATE_ISOLATED with no NAT
        # Gateway and no route to an IGW at all (SPEC.md's "one NAT gateway
        # total, in app-vpc" -- provider-vpc gets none) -- confirmed the
        # hard way: the Fargate task failed with "unable to pull registry
        # auth from Amazon ECR: ... dial tcp ...:443: i/o timeout" because
        # it had no path to ECR's API at all. ECR API (auth), ECR Docker
        # (image manifest/layers), CloudWatch Logs (the aws_logs driver
        # below), and S3 (ECR's layer storage backend) are the minimum set
        # for a Fargate task in a fully-isolated subnet. Scoped to `self`
        # explicitly (not provider_vpc.add_interface_endpoint(...), which
        # would anchor the new resource in NetworkStack's tree instead --
        # same cross-stack-scoping class of bug as inspection_stack.py's
        # CfnRoute comment explains).
        # ------------------------------------------------------------------
        vpc_endpoint_sg = ec2.SecurityGroup(
            self, "ProviderVpcEndpointSg",
            vpc=provider_vpc,
            description="Interface endpoints (ECR/Logs) for the Fargate provider task -- inbound from provider-vpc only",
            allow_all_outbound=True,
        )
        vpc_endpoint_sg.add_ingress_rule(
            ec2.Peer.ipv4(provider_vpc.vpc_cidr_block), ec2.Port.tcp(443), "provider-vpc"
        )
        for service_id, service in {
            "Ecr": ec2.InterfaceVpcEndpointAwsService.ECR,
            "EcrDocker": ec2.InterfaceVpcEndpointAwsService.ECR_DOCKER,
            "Logs": ec2.InterfaceVpcEndpointAwsService.CLOUDWATCH_LOGS,
        }.items():
            ec2.InterfaceVpcEndpoint(
                self, f"Provider{service_id}Endpoint",
                vpc=provider_vpc,
                service=service,
                subnets=ec2.SubnetSelection(subnet_group_name="Private"),
                security_groups=[vpc_endpoint_sg],
            )
        ec2.GatewayVpcEndpoint(
            self, "ProviderS3Endpoint",
            vpc=provider_vpc,
            service=ec2.GatewayVpcEndpointAwsService.S3,
            subnets=[ec2.SubnetSelection(subnet_group_name="Private")],
        )

        # ------------------------------------------------------------------
        # Provider app: ECS Fargate, smallest task size. See
        # threetier_stack.py's module docstring for why Fargate over EC2
        # (separate vCPU quota pool, no host patching) -- the same reasoning
        # applies here, and unlike threetier_stack.py's app tier, nothing
        # references this instance's identity directly (no VPC Lattice
        # INSTANCE-type target group involved), so no dedicated EC2 host is
        # needed to preserve anything.
        # ------------------------------------------------------------------
        provider_cluster = ecs.Cluster(
            self, "ProviderCluster", vpc=provider_vpc, container_insights_v2=ecs.ContainerInsights.DISABLED
        )

        provider_task_definition = ecs.FargateTaskDefinition(
            self, "ProviderTaskDefinition",
            cpu=256,
            memory_limit_mib=512,
        )
        provider_task_definition.add_container(
            "ProviderContainer",
            image=ecs.ContainerImage.from_asset(str(PROVIDER_ASSETS_DIR)),
            port_mappings=[ecs.PortMapping(container_port=PROVIDER_PORT)],
            environment={"PORT": str(PROVIDER_PORT)},
            logging=ecs.LogDrivers.aws_logs(stream_prefix="provider", log_retention=logs.RetentionDays.ONE_WEEK),
        )

        provider_service = ecs.FargateService(
            self, "ProviderService",
            cluster=provider_cluster,
            task_definition=provider_task_definition,
            desired_count=2 if config.MULTI_AZ else 1,
            vpc_subnets=ec2.SubnetSelection(subnet_group_name="Private"),
            security_groups=[provider_sg],
            enable_execute_command=True,
            circuit_breaker=ecs.DeploymentCircuitBreaker(rollback=True),
            min_healthy_percent=0,
            max_healthy_percent=200,
        )

        # ------------------------------------------------------------------
        # NLB (internal) + target group -- a plain, correct NLB use,
        # unrelated to inspection_stack.py's GWLB (see this file's module
        # docstring). IP target type, not INSTANCE -- same reason as
        # threetier_stack.py's ALB target group (Fargate awsvpc mode has no
        # host instance to register by id).
        #
        # Explicit security group -- NLBs now support them (a newer
        # AWS/CDK feature; classic NLBs never needed one), and when you
        # don't pass one, CDK auto-creates a locked-down placeholder with
        # zero ingress and no real egress. Confirmed the hard way: the
        # Fargate task ran fine (no crash, no exit code) but its NLB
        # target group health check failed continuously -- the NLB's own
        # auto-generated security group couldn't send it any traffic at
        # all, health check included.
        # ------------------------------------------------------------------
        nlb_sg = ec2.SecurityGroup(
            self, "ProviderNlbSg",
            vpc=provider_vpc,
            description="Provider NLB -- inbound from provider-vpc (PrivateLink consumers), outbound to the Fargate target",
            allow_all_outbound=True,
        )
        nlb_sg.add_ingress_rule(
            ec2.Peer.ipv4(provider_vpc.vpc_cidr_block), ec2.Port.tcp(PROVIDER_PORT), "provider-vpc"
        )
        nlb = elbv2.NetworkLoadBalancer(
            self, "ProviderNlb",
            vpc=provider_vpc,
            internet_facing=False,
            vpc_subnets=ec2.SubnetSelection(subnet_group_name="Private"),
            security_groups=[nlb_sg],
        )
        provider_target_group = elbv2.NetworkTargetGroup(
            self, "ProviderTargetGroup",
            vpc=provider_vpc,
            port=PROVIDER_PORT,
            protocol=elbv2.Protocol.TCP,
            target_type=elbv2.TargetType.IP,
        )
        provider_service.attach_to_network_target_group(provider_target_group)
        nlb.add_listener(
            "ProviderListener",
            port=PROVIDER_PORT,
            protocol=elbv2.Protocol.TCP,
            default_action=elbv2.NetworkListenerAction.forward([provider_target_group]),
        )

        # ------------------------------------------------------------------
        # VPC Endpoint Service fronting the NLB. acceptance_required=False:
        # single-account lab, not a cross-account PrivateLink offering that
        # needs a manual accept step.
        # ------------------------------------------------------------------
        endpoint_service = ec2.CfnVPCEndpointService(
            self, "ProviderEndpointService",
            network_load_balancer_arns=[nlb.load_balancer_arn],
            acceptance_required=False,
        )
        service_name = f"com.amazonaws.vpce.{self.region}.{endpoint_service.attr_service_id}"

        # ------------------------------------------------------------------
        # Interface endpoint in app-vpc -- the consumer side. private_dns
        # is left off (needs domain-ownership verification not practical
        # for a lab); consumers resolve the endpoint via its own generated
        # DNS name (see the CfnOutput below) rather than the service's
        # "friendly" name.
        # ------------------------------------------------------------------
        consumer_sg = ec2.SecurityGroup(
            self, "ConsumerEndpointSg",
            vpc=app_vpc,
            description="Interface endpoint for the PrivateLink provider -- inbound from app-vpc only",
            allow_all_outbound=True,
        )
        consumer_sg.add_ingress_rule(
            ec2.Peer.ipv4(app_vpc.vpc_cidr_block), ec2.Port.tcp(PROVIDER_PORT), "app-vpc"
        )

        # A VPC Endpoint Service only supports the AZs its backing NLB
        # actually has nodes in (provider_vpc's own AZ count, independent of
        # app_vpc's -- app_vpc is fixed at >=2 AZs per network_stack.py,
        # provider_vpc has no such forcing and stays on the general
        # MULTI_AZ-driven az_count).
        # Selecting all of app_vpc's Private subnets unrestricted would try
        # to place an ENI in an AZ the service doesn't support ("does not
        # support the availability zone of the subnet") whenever the two
        # VPCs' AZ counts differ -- restrict to the AZs the NLB is actually
        # in.
        nlb_azs = [s.availability_zone for s in provider_vpc.select_subnets(subnet_group_name="Private").subnets]
        interface_endpoint = ec2.InterfaceVpcEndpoint(
            self, "ProviderInterfaceEndpoint",
            vpc=app_vpc,
            service=ec2.InterfaceVpcEndpointService(service_name, PROVIDER_PORT),
            subnets=ec2.SubnetSelection(subnet_group_name="Private", availability_zones=nlb_azs),
            security_groups=[consumer_sg],
            private_dns_enabled=False,
        )
        interface_endpoint.node.add_dependency(endpoint_service)

        # ------------------------------------------------------------------
        # Outputs -- for the verify step: from an SSM session on app-vpc's
        # test host (network_stack.py), curl the endpoint's DNS name on
        # PROVIDER_PORT and confirm the provider's response arrives, with no
        # route to provider-vpc's CIDR anywhere in app-vpc's route tables.
        # ------------------------------------------------------------------
        CfnOutput(self, "InterfaceEndpointId", value=interface_endpoint.vpc_endpoint_id)
        CfnOutput(
            self, "InterfaceEndpointDnsName",
            value=cdk.Fn.select(1, cdk.Fn.split(":", cdk.Fn.select(0, interface_endpoint.vpc_endpoint_dns_entries))),
        )
        CfnOutput(self, "ProviderServiceName", value=service_name)

        # ------------------------------------------------------------------
        # cdk-nag suppressions (SPEC.md Section 10)
        # ------------------------------------------------------------------
        NagSuppressions.add_stack_suppressions(self, [
            NagPackSuppression(
                id="AwsSolutions-IAM4",
                reason="AmazonECSTaskExecutionRolePolicy is AWS-curated and is the standard execution "
                       "role for Fargate tasks (image pull + log group write) -- not a hand-widened grant.",
            ),
            NagPackSuppression(
                id="AwsSolutions-IAM5",
                reason="Wildcards come from CDK's own Fargate task-definition-generated execution role "
                       "policy (ECR/CloudWatch Logs access scoped by the framework, not hand-widened) and "
                       "the ECS Exec (enable_execute_command) SSM channel permissions.",
            ),
            NagPackSuppression(
                id="AwsSolutions-ELB2",
                reason="NLB access logging needs a dedicated log bucket -- not worth the added "
                       "infrastructure for this lab's internal provider-side NLB.",
            ),
            NagPackSuppression(
                id="AwsSolutions-ECS4",
                reason="Container Insights is extra CloudWatch cost with no security value for a lab "
                       "meant to be built and torn down repeatedly.",
            ),
            NagPackSuppression(
                id="AwsSolutions-ECS2",
                reason="The only 'environment variable' is PORT=8080, a fixed, non-sensitive listen "
                       "port -- not the kind of value AwsSolutions-ECS2 exists to catch (secrets/config "
                       "that belong in Secrets Manager or SSM Parameter Store instead).",
            ),
        ])
        NagSuppressions.add_resource_suppressions(
            [provider_sg, consumer_sg],
            [NagPackSuppression(
                id="AwsSolutions-EC23",
                reason="cdk-nag can't statically resolve vpc.vpc_cidr_block (a CloudFormation token) to "
                       "confirm it isn't 0.0.0.0/0 -- both SGs are scoped to their own VPC's CIDR only, "
                       "not open access.",
            )],
        )
