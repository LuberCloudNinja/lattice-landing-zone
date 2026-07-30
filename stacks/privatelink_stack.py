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

import aws_cdk as cdk
from aws_cdk import CfnOutput, Stack, Tags
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_elasticloadbalancingv2 as elbv2
from aws_cdk import aws_elasticloadbalancingv2_targets as elbv2_targets
from aws_cdk import aws_iam as iam
from cdk_nag import NagPackSuppression, NagSuppressions
from constructs import Construct

import config
from stacks.network_stack import NetworkStack

PROVIDER_PORT = 8080


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

        provider_role = iam.Role(
            self, "ProviderInstanceRole",
            assumed_by=iam.ServicePrincipal("ec2.amazonaws.com"),
            managed_policies=[iam.ManagedPolicy.from_aws_managed_policy_name("AmazonSSMManagedInstanceCore")],
        )

        provider_user_data = ec2.UserData.for_linux()
        provider_user_data.add_commands(
            "set -e",
            "mkdir -p /opt/provider",
            "cat > /opt/provider/app.py << 'PYEOF'\n"
            "import json\n"
            "from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer\n"
            "\n"
            "class Handler(BaseHTTPRequestHandler):\n"
            "    def do_GET(self):\n"
            "        body = json.dumps({\"message\": \"hello from the PrivateLink provider\", \"path\": self.path}).encode()\n"
            "        self.send_response(200)\n"
            "        self.send_header(\"Content-Type\", \"application/json\")\n"
            "        self.send_header(\"Content-Length\", str(len(body)))\n"
            "        self.end_headers()\n"
            "        self.wfile.write(body)\n"
            "\n"
            f"ThreadingHTTPServer((\"0.0.0.0\", {PROVIDER_PORT}), Handler).serve_forever()\n"
            "PYEOF",
            "cat > /etc/systemd/system/provider-app.service << 'EOF'\n"
            "[Unit]\n"
            "Description=PrivateLink demo provider app\n"
            "After=network.target\n"
            "[Service]\n"
            "ExecStart=/usr/bin/python3 /opt/provider/app.py\n"
            "Restart=always\n"
            "[Install]\n"
            "WantedBy=multi-user.target\n"
            "EOF",
            "systemctl daemon-reload",
            "systemctl enable --now provider-app",
        )

        provider_instance = ec2.Instance(
            self, "ProviderInstance",
            vpc=provider_vpc,
            vpc_subnets=ec2.SubnetSelection(subnet_group_name="Private"),
            instance_type=ec2.InstanceType(config.DEFAULT_INSTANCE_TYPE),
            machine_image=ec2.MachineImage.latest_amazon_linux2023(),
            security_group=provider_sg,
            role=provider_role,
            user_data=provider_user_data,
            ssm_session_permissions=True,
            block_devices=[ec2.BlockDevice(
                device_name="/dev/xvda", volume=ec2.BlockDeviceVolume.ebs(8, encrypted=True)
            )],
        )

        # ------------------------------------------------------------------
        # NLB (internal) + target group, targeting the provider instance by
        # ID -- a plain, correct NLB use, unrelated to inspection_stack.py's
        # GWLB (see this file's module docstring).
        # ------------------------------------------------------------------
        nlb = elbv2.NetworkLoadBalancer(
            self, "ProviderNlb",
            vpc=provider_vpc,
            internet_facing=False,
            vpc_subnets=ec2.SubnetSelection(subnet_group_name="Private"),
        )
        provider_target_group = elbv2.NetworkTargetGroup(
            self, "ProviderTargetGroup",
            vpc=provider_vpc,
            port=PROVIDER_PORT,
            protocol=elbv2.Protocol.TCP,
            target_type=elbv2.TargetType.INSTANCE,
            targets=[elbv2_targets.InstanceTarget(provider_instance)],
        )
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
        # app_vpc's -- app_vpc needs >=2 AZs for RDS's DBSubnetGroup
        # requirement per network_stack.py, provider_vpc has no such forced
        # requirement and stays on the general MULTI_AZ-driven az_count).
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
                reason="AmazonSSMManagedInstanceCore is AWS-curated and appropriate for this lab's "
                       "SSM-only instance access.",
            ),
            NagPackSuppression(
                id="AwsSolutions-EC28",
                reason="Detailed monitoring is extra cost with no security value for a lab instance.",
            ),
            NagPackSuppression(
                id="AwsSolutions-EC29",
                reason="Termination protection would conflict with SPEC.md's `cdk destroy --all` "
                       "teardown requirement.",
            ),
            NagPackSuppression(
                id="AwsSolutions-ELB2",
                reason="NLB access logging needs a dedicated log bucket -- not worth the added "
                       "infrastructure for this lab's internal provider-side NLB.",
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
