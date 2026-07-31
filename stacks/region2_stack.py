"""region2_stack.py -- us-east-2 footprint attaching into the Cloud WAN core network.

Deployed only when config.ENABLE_CLOUDWAN is on (stage.py passes
env=us-east-2 + cross_region_references=True). One small "prod Workload"
VPC (config.REGION2_VPC_CIDRS["region2_prod"]), PRIVATE_ISOLATED subnets
only (no NAT/IGW at all -- Session Manager over local interface endpoints
is the only access path, matching this project's existing "SSM-only, no
bastion" pattern used everywhere else), and a CfnVpcAttachment into the
Workload segment.

Cross-region note: security_stack.py's CMKs are regional to us-east-1 and
cannot encrypt resources created in us-east-2 (KMS keys don't span
regions without an explicit multi-region key setup, out of scope for this
lab) -- this stack's EBS volume uses the AWS-managed default key instead,
a deliberate, documented exception to the rest of the project's
CMK-everywhere pattern, not an oversight.
"""

import aws_cdk as cdk
from aws_cdk import CfnOutput, Stack, Tags
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_iam as iam
from aws_cdk import aws_networkmanager as nm
from cdk_nag import NagPackSuppression, NagSuppressions
from constructs import Construct

import config


class Region2Stack(Stack):
    """us-east-2: one Workload-segment VPC + SSM test instance, attached to the Cloud WAN core network."""

    def __init__(self, scope: Construct, construct_id: str, *, core_network_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)
        Tags.of(self).add("Layer", config.Layer.REGION2)

        self.vpc = ec2.Vpc(
            self, "Region2ProdVpc",
            vpc_name="region2-prod-vpc",
            ip_addresses=ec2.IpAddresses.cidr(config.REGION2_VPC_CIDRS["region2_prod"]),
            max_azs=1,
            nat_gateways=0,
            flow_logs={"ToCloudWatch": ec2.FlowLogOptions()},
            subnet_configuration=[
                ec2.SubnetConfiguration(name="Private", subnet_type=ec2.SubnetType.PRIVATE_ISOLATED, cidr_mask=24),
            ],
        )

        endpoint_sg = ec2.SecurityGroup(
            self, "EndpointSg",
            vpc=self.vpc,
            description="SSM interface endpoints -- inbound HTTPS from this VPC only",
            allow_all_outbound=True,
        )
        endpoint_sg.add_ingress_rule(ec2.Peer.ipv4(self.vpc.vpc_cidr_block), ec2.Port.tcp(443), "HTTPS from region2-prod-vpc")
        for service_name, service in {
            "Ssm": ec2.InterfaceVpcEndpointAwsService.SSM,
            "SsmMessages": ec2.InterfaceVpcEndpointAwsService.SSM_MESSAGES,
            "Ec2Messages": ec2.InterfaceVpcEndpointAwsService.EC2_MESSAGES,
        }.items():
            self.vpc.add_interface_endpoint(
                f"{service_name}Endpoint", service=service,
                subnets=ec2.SubnetSelection(subnet_group_name="Private"),
                security_groups=[endpoint_sg],
            )

        test_instance_role = iam.Role(
            self, "TestInstanceRole",
            assumed_by=iam.ServicePrincipal("ec2.amazonaws.com"),
            managed_policies=[iam.ManagedPolicy.from_aws_managed_policy_name("AmazonSSMManagedInstanceCore")],
        )
        test_instance_sg = ec2.SecurityGroup(
            self, "TestInstanceSg", vpc=self.vpc,
            description="region2 SSM test instance -- no inbound needed (SSM is outbound-initiated)",
            allow_all_outbound=True,
        )
        self.test_instance = ec2.Instance(
            self, "TestInstance",
            vpc=self.vpc,
            vpc_subnets=ec2.SubnetSelection(subnet_group_name="Private"),
            instance_type=ec2.InstanceType(config.DEFAULT_INSTANCE_TYPE),
            machine_image=ec2.MachineImage.latest_amazon_linux2023(),
            security_group=test_instance_sg,
            role=test_instance_role,
            ssm_session_permissions=True,
            block_devices=[ec2.BlockDevice(device_name="/dev/xvda", volume=ec2.BlockDeviceVolume.ebs(8, encrypted=True))],
        )

        attach_subnets = self.vpc.select_subnets(subnet_group_name="Private").subnets
        self.vpc_attachment = nm.CfnVpcAttachment(
            self, "Region2VpcAttachment",
            core_network_id=core_network_id,
            vpc_arn=self.format_arn(service="ec2", resource="vpc", resource_name=self.vpc.vpc_id),
            subnet_arns=[self.format_arn(service="ec2", resource="subnet", resource_name=s.subnet_id) for s in attach_subnets],
            tags=[cdk.CfnTag(key="segment", value=config.CloudWanSegment.WORKLOAD)],
        )

        CfnOutput(self, "Region2VpcId", value=self.vpc.vpc_id)
        CfnOutput(self, "TestInstanceId", value=self.test_instance.instance_id)
        CfnOutput(self, "Region2VpcAttachmentId", value=self.vpc_attachment.attr_attachment_id)

        # ------------------------------------------------------------------
        # cdk-nag suppressions (SPEC.md Section 10) -- same reasoning
        # already established project-wide for this exact pattern (see
        # network_stack.py's stack suppressions).
        # ------------------------------------------------------------------
        NagSuppressions.add_stack_suppressions(self, [
            NagPackSuppression(
                id="AwsSolutions-IAM4",
                reason="AmazonSSMManagedInstanceCore is an AWS-curated policy appropriate for SSM-only "
                       "instance access in this lab; no custom policy adds real security value here.",
            ),
            NagPackSuppression(
                id="AwsSolutions-EC28",
                reason="Detailed EC2 monitoring is extra CloudWatch cost with no security value for a "
                       "lab meant to be built and torn down repeatedly.",
            ),
            NagPackSuppression(
                id="AwsSolutions-EC29",
                reason="Termination protection would directly conflict with SPEC.md's `cdk destroy "
                       "--all` teardown requirement -- this project is explicitly designed to be fully "
                       "destroyable.",
            ),
        ])
