"""inspection_stack.py -- GWLB + firewall appliances (2 per AZ, multi-AZ) + endpoints + appliance-mode.

See SPEC.md Section 6 ("inspection_stack.py (GWLB, HA)") and, in full detail,
docs/inspection-architecture-reference.md Sections 4-5 -- both were read (and
one, corrected) while building this file. Corrections made along the way,
verified against live AWS documentation and the real upstream gwlbtun repo
rather than assumed:

  - There is NO target-group-level "Appliance Mode" boolean attribute (an
    earlier research pass, before this project had live web access, claimed
    there was). Flow symmetry is: (a) the Inspection VPC's TGW attachment
    `appliance_mode_support=enable` (NetworkStack), which is the *only*
    Appliance Mode switch in this design, plus (b) leaving this stack's GWLB
    target group at its DEFAULT 5-tuple stickiness -- 2-/3-tuple stickiness
    is explicitly documented as incompatible with TGW Appliance Mode, not an
    alternative way to express it. See docs/inspection-architecture-reference.md
    Section 4.4's correction note for the full explanation.
  - gwlbtun's confirmed health-check port is 8080 (GWLBTUN_HEALTHCHECK_PORT
    below, must match stacks/assets/inspection/install-gwlbtun.sh).
"""

from pathlib import Path

import aws_cdk as cdk
from aws_cdk import CfnOutput, Duration, Stack, Tags
from aws_cdk import aws_autoscaling as autoscaling
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_elasticloadbalancingv2 as elbv2
from aws_cdk import aws_iam as iam
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_s3_assets as s3_assets
from aws_cdk import aws_s3_deployment as s3_deploy
from aws_cdk import aws_ssm as ssm
from cdk_nag import NagPackSuppression, NagSuppressions
from constructs import Construct

import config
from stacks.network_stack import NetworkStack

ASSETS_DIR = Path(__file__).parent / "assets" / "inspection"

# Must match GWLBTUN_HEALTHCHECK_PORT in stacks/assets/inspection/install-gwlbtun.sh --
# confirmed against the real aws-gateway-load-balancer-tunnel-handler repo
# during the artifact-drafting workflow, not guessed.
GWLBTUN_HEALTHCHECK_PORT = 8080

# Canonical's official SSM parameter path for the latest Ubuntu 24.04 LTS
# AMI -- see docs/inspection-architecture-reference.md Section 5.4 for why
# Ubuntu 24.04 (Suricata packaging/patch cadence) over Amazon Linux 2023.
UBUNTU_2404_SSM_PARAM = "/aws/service/canonical/ubuntu/server/24.04/stable/current/amd64/hvm/ebs-gp3/ami-id"

SSM_PL_VERSION_PARAM = "/lattice-lab/inspection/pl-version"
S3_POLICY_PREFIX = "policy"


class InspectionStack(Stack):
    """GWLB target group (GENEVE/6081) fronting 2 firewall appliances per AZ, HA."""

    def __init__(self, scope: Construct, construct_id: str, *, network: NetworkStack, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)
        Tags.of(self).add("Layer", config.Layer.INSPECTION)

        vpc = network.inspection_vpc
        app_vpc = network.app_vpc

        firewall_subnets = vpc.select_subnets(subnet_group_name="Firewall").subnets
        gwlb_endpoint_subnets = vpc.select_subnets(subnet_group_name="GwlbEndpoint").subnets
        tgw_attach_subnets = vpc.select_subnets(subnet_group_name="TgwAttach").subnets

        # ------------------------------------------------------------------
        # Policy-as-code distribution: versioned S3 bucket + SSM version
        # pointer (reference doc Section 5.5) -- pl-sync.sh (baked into the
        # software asset below) polls SSM_PL_VERSION_PARAM and, on change,
        # pulls policy.yaml/zone-definitions.yaml from this bucket.
        # ------------------------------------------------------------------
        policy_bucket = s3.Bucket(
            self, "PolicyBucket",
            versioned=True,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            removal_policy=cdk.RemovalPolicy.DESTROY,
            auto_delete_objects=True,
        )
        s3_deploy.BucketDeployment(
            self, "PolicyDeployment",
            sources=[s3_deploy.Source.asset(str(ASSETS_DIR / "policy-bucket"))],
            destination_bucket=policy_bucket,
            destination_key_prefix=S3_POLICY_PREFIX,
        )
        pl_version_param = ssm.StringParameter(
            self, "PlVersionParam",
            parameter_name=SSM_PL_VERSION_PARAM,
            string_value="v1",
            description="Policy-as-code version pointer -- pl-sync.sh polls this; bump it after publishing a new policy.yaml/zone-definitions.yaml to PolicyBucket.",
        )

        # ------------------------------------------------------------------
        # Firewall SOFTWARE asset bundle (gwlbtun/Suricata installers,
        # generate_nft.py, pl-sync.sh + units) -- deliberately separate from
        # the policy bucket above (software = baked in at boot, policy =
        # pulled dynamically thereafter). Uploaded as a single zip via the
        # CDK asset system since these files total ~85KB, far over EC2
        # user-data's 16KB limit -- user-data only ever contains a short
        # script that downloads and extracts this bundle, never the fragment
        # contents themselves.
        # ------------------------------------------------------------------
        software_asset = s3_assets.Asset(
            self, "FirewallSoftwareAsset",
            path=str(ASSETS_DIR),
            exclude=["policy-bucket/*", "policy-bucket", "policy.yaml", "zone-definitions.yaml"],
        )

        # ------------------------------------------------------------------
        # Security group -- GWLB has no narrow, whitelistable source IPs of
        # its own; the real security boundary is the firewall's own nftables
        # policy (the "PL"), not this SG. Inbound only from inside
        # inspection-vpc: GENEVE/6081 (GWLB data plane) + the gwlbtun
        # health-check port (GWLB's health checker also originates from
        # inside the VPC).
        # ------------------------------------------------------------------
        firewall_sg = ec2.SecurityGroup(
            self, "FirewallSg",
            vpc=vpc,
            description="Firewall fleet -- GENEVE/6081 + gwlbtun health check from inspection-vpc",
            allow_all_outbound=True,
        )
        firewall_sg.add_ingress_rule(ec2.Peer.ipv4(vpc.vpc_cidr_block), ec2.Port.udp(6081), "GENEVE from GWLB")
        firewall_sg.add_ingress_rule(
            ec2.Peer.ipv4(vpc.vpc_cidr_block), ec2.Port.tcp(GWLBTUN_HEALTHCHECK_PORT), "gwlbtun health check"
        )

        # ------------------------------------------------------------------
        # IAM -- least-privilege per reference doc Section 5.5: SSM session
        # (no SSH), read-only on the specific policy-bucket prefix and SSM
        # parameter (not broad S3/SSM access), CloudWatch Logs for
        # Suricata/nftables log shipping (Section 5.3), read on the software
        # asset bundle.
        # ------------------------------------------------------------------
        firewall_role = iam.Role(
            self, "FirewallRole",
            assumed_by=iam.ServicePrincipal("ec2.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name("AmazonSSMManagedInstanceCore"),
                iam.ManagedPolicy.from_aws_managed_policy_name("CloudWatchAgentServerPolicy"),
            ],
        )
        policy_bucket.grant_read(firewall_role, f"{S3_POLICY_PREFIX}/*")
        pl_version_param.grant_read(firewall_role)
        software_asset.grant_read(firewall_role)

        # ------------------------------------------------------------------
        # Launch Template: download + extract the software bundle, wire the
        # real S3/SSM values pl-sync.sh needs, then SOURCE (not execute) the
        # install fragments from inside bash functions.
        #
        # Why `source`, not just running the extracted .sh files directly:
        # install-gwlbtun.sh / install-suricata.sh use `return 1 ... || true`
        # to abort early on a fatal step (missing binary, failed build) --
        # `return` is only meaningful inside a function or a script that is
        # itself SOURCED (not run as a subprocess). The artifact-verification
        # pass that built these files explicitly flagged this as needing a
        # decision here rather than a change to the fragments themselves
        # (see their own summaries) -- sourcing each one from inside a
        # wrapper function is that decision: it makes their existing
        # abort-on-failure logic behave exactly as the fragments' own authors
        # intended, and lets main() below gate the whole boot on each step's
        # real exit status.
        # ------------------------------------------------------------------
        user_data = ec2.UserData.for_linux()
        user_data.add_commands(
            "#!/bin/bash",
            "set -u",
            "exec > >(tee -a /var/log/inspection-bootstrap.log) 2>&1",
            "echo \"=== inspection-fleet bootstrap starting $(date -u) ===\"",
            "apt-get update -y",
            "apt-get install -y unzip awscli nftables",
            "mkdir -p /opt/inspection",
        )
        local_zip = "/tmp/inspection-assets.zip"
        user_data.add_s3_download_command(
            bucket=software_asset.bucket,
            bucket_key=software_asset.s3_object_key,
            local_file=local_zip,
            region=self.region,
        )
        user_data.add_commands(
            f"unzip -o {local_zip} -d /opt/inspection",
            "chmod +x /opt/inspection/*.sh /opt/inspection/generate_nft.py /opt/inspection/pl-sync.sh",
            "cp /opt/inspection/pl-sync.service /etc/systemd/system/pl-sync.service",
            "cp /opt/inspection/pl-sync.timer /etc/systemd/system/pl-sync.timer",
            "cat > /etc/default/pl-sync << EOF\n"
            f"PL_SSM_PARAM={SSM_PL_VERSION_PARAM}\n"
            f"S3_BUCKET={policy_bucket.bucket_name}\n"
            f"S3_PREFIX={S3_POLICY_PREFIX}\n"
            "EOF",
            "",
            "install_gwlbtun() {",
            "  source /opt/inspection/install-gwlbtun.sh",
            "}",
            "install_suricata() {",
            "  source /opt/inspection/install-suricata.sh",
            "}",
            "",
            'install_gwlbtun || { echo "FATAL: install_gwlbtun failed"; exit 1; }',
            'install_suricata || { echo "FATAL: install_suricata failed"; exit 1; }',
            "",
            "systemctl enable nftables",
            "systemctl start nftables",
            "systemctl daemon-reload",
            "",
            "# Synchronous first PL apply BEFORE the ASG health check can pass --",
            "# pl-sync.timer alone is asynchronous and would leave a window where",
            "# the instance is marked healthy with no policy loaded at all (a gap",
            "# the artifact-verification pass explicitly flagged; this is the fix).",
            '/opt/inspection/pl-sync.sh || { echo "FATAL: initial pl-sync failed"; exit 1; }',
            "systemctl enable --now pl-sync.timer",
            "",
            "echo \"=== inspection-fleet bootstrap complete $(date -u) ===\"",
        )

        launch_template = ec2.LaunchTemplate(
            self, "FirewallLaunchTemplate",
            instance_type=ec2.InstanceType(config.DEFAULT_INSTANCE_TYPE),
            machine_image=ec2.MachineImage.from_ssm_parameter(
                UBUNTU_2404_SSM_PARAM, os=ec2.OperatingSystemType.LINUX
            ),
            user_data=user_data,
            role=firewall_role,
            security_group=firewall_sg,
            require_imdsv2=True,
            # 20GB, not the 8GB default -- building gwlbtun from source (Boost
            # headers + CMake + compiler) and Suricata's ET Open ruleset both
            # need real room. Encrypted per cdk-nag AwsSolutions-EC26.
            block_devices=[ec2.BlockDevice(
                device_name="/dev/sda1", volume=ec2.BlockDeviceVolume.ebs(20, encrypted=True)
            )],
        )

        # ------------------------------------------------------------------
        # GWLB target group. See this file's module docstring / reference
        # doc Section 4.4's correction note: default 5-tuple stickiness
        # (no stickiness.* attributes set) is the CORRECT, TGW-Appliance-Mode
        # -compatible configuration -- not a target-group Appliance Mode
        # toggle, which does not exist.
        # ------------------------------------------------------------------
        # Exposed publicly -- observability_stack.py's dashboard references
        # both of these for GWLB/target-health widgets.
        self.target_group = target_group = elbv2.CfnTargetGroup(
            self, "FirewallTargetGroup",
            vpc_id=vpc.vpc_id,
            protocol="GENEVE",
            port=6081,
            target_type="instance",
            health_check_protocol="TCP",
            health_check_port=str(GWLBTUN_HEALTHCHECK_PORT),
            health_check_interval_seconds=10,
            healthy_threshold_count=3,
            unhealthy_threshold_count=3,
            target_group_attributes=[
                # 2 appliances per AZ exist specifically for HA -- rebalance
                # existing flows onto the surviving healthy target rather
                # than the AWS default (no_rebalance), which would black-hole
                # in-flight flows until the ASG replaces the instance.
                elbv2.CfnTargetGroup.TargetGroupAttributeProperty(
                    key="target_failover.on_deregistration", value="rebalance"
                ),
                elbv2.CfnTargetGroup.TargetGroupAttributeProperty(
                    key="target_failover.on_unhealthy", value="rebalance"
                ),
            ],
        )

        self.gwlb = gwlb = elbv2.CfnLoadBalancer(
            self, "Gwlb",
            type="gateway",
            subnets=[s.subnet_id for s in firewall_subnets],
        )

        elbv2.CfnListener(
            self, "GwlbListener",
            load_balancer_arn=gwlb.attr_load_balancer_arn,
            default_actions=[elbv2.CfnListener.ActionProperty(type="forward", target_group_arn=target_group.ref)],
        )

        # ------------------------------------------------------------------
        # VPC Endpoint Service (fronting the GWLB) + one GWLB Endpoint per AZ.
        # acceptance_required=False: this is a single-account lab, not a
        # cross-account PrivateLink offering -- no manual accept step needed.
        # ------------------------------------------------------------------
        endpoint_service = ec2.CfnVPCEndpointService(
            self, "GwlbEndpointService",
            gateway_load_balancer_arns=[gwlb.attr_load_balancer_arn],
            acceptance_required=False,
        )
        # AWS::EC2::VPCEndpointService has no ServiceName GetAtt -- construct
        # it from the documented, stable com.amazonaws.vpce.<region>.<service-id>
        # format using the real attr_service_id token.
        service_name = f"com.amazonaws.vpce.{self.region}.{endpoint_service.attr_service_id}"

        gwlb_endpoints: list[ec2.CfnVPCEndpoint] = []
        for i, gwlbe_subnet in enumerate(gwlb_endpoint_subnets):
            ep = ec2.CfnVPCEndpoint(
                self, f"GwlbEndpoint{i}",
                vpc_id=vpc.vpc_id,
                vpc_endpoint_type="GatewayLoadBalancer",
                service_name=service_name,
                subnet_ids=[gwlbe_subnet.subnet_id],
            )
            ep.node.add_dependency(endpoint_service)
            gwlb_endpoints.append(ep)

        # TgwAttach subnet route -> the SAME-AZ GWLB Endpoint (matched by AZ
        # name, not list position -- both subnet groups are built from the
        # same VPC's AZ list so position happens to line up here, but
        # matching explicitly is what's actually correct).
        #
        # Deliberately NOT using tgw_subnet.add_route(...) here: that
        # convenience method creates the new CfnRoute as a CHILD of the
        # subnet construct -- which lives in NetworkStack's tree, since
        # these subnets were created there. A NetworkStack-owned resource
        # referencing an InspectionStack resource (this endpoint) while
        # InspectionStack already depends on NetworkStack for the VPC/subnet
        # objects themselves is a real dependency cycle (hit this on the
        # first synth attempt). Creating the CfnRoute explicitly scoped to
        # `self` (InspectionStack) keeps the dependency one-directional.
        for tgw_subnet in tgw_attach_subnets:
            match = next(
                (ep, gwlbe_subnet) for ep, gwlbe_subnet in zip(gwlb_endpoints, gwlb_endpoint_subnets)
                if gwlbe_subnet.availability_zone == tgw_subnet.availability_zone
            )
            endpoint, _ = match
            route = ec2.CfnRoute(
                self, f"{tgw_subnet.node.id}DefaultToGwlbEndpoint",
                route_table_id=tgw_subnet.route_table.route_table_id,
                destination_cidr_block="0.0.0.0/0",
                vpc_endpoint_id=endpoint.ref,
            )
            route.node.add_dependency(endpoint)

        # ------------------------------------------------------------------
        # IGW ingress (edge-association) route table in app-vpc -- the piece
        # most commonly missed. Without it, inbound reply packets for
        # outbound-initiated (north-south) sessions arrive at app-vpc's IGW
        # and route straight to the NAT Gateway via the normal per-subnet
        # route table, skipping inspection on the return leg entirely.
        #
        # Matched against the app-vpc Public subnet's whole CIDR (that
        # subnet contains only the NAT Gateway, nothing else) rather than the
        # NAT Gateway's specific private IP -- CfnNatGateway doesn't expose
        # its private IP as a CDK attribute, and a subnet-CIDR match is
        # exactly equivalent here while avoiding an extra runtime lookup.
        # ------------------------------------------------------------------
        ingress_rt = ec2.CfnRouteTable(self, "AppVpcIngressRouteTable", vpc_id=app_vpc.vpc_id)
        ec2.CfnGatewayRouteTableAssociation(
            self, "AppVpcIgwIngressAssoc",
            gateway_id=app_vpc.internet_gateway_id,
            route_table_id=ingress_rt.ref,
        )

        for pub_subnet in app_vpc.select_subnets(subnet_group_name="Public").subnets:
            match = next(
                (ep for ep, gwlbe_subnet in zip(gwlb_endpoints, gwlb_endpoint_subnets)
                 if gwlbe_subnet.availability_zone == pub_subnet.availability_zone),
                None,
            )
            if match is None:
                # inspection-vpc has more AZs than app-vpc currently has NAT
                # gateways in (e.g. MULTI_AZ=false, config.INSPECTION_AZ_COUNT=2)
                # -- nothing to redirect for an AZ with no NAT/Public subnet.
                continue
            ec2.CfnRoute(
                self, f"IngressRedirect{pub_subnet.node.id}",
                route_table_id=ingress_rt.ref,
                destination_cidr_block=pub_subnet.ipv4_cidr_block,
                vpc_endpoint_id=match.ref,
            )

        # ------------------------------------------------------------------
        # Firewall fleet: one ASG PER AZ (not one shared multi-AZ ASG -- a
        # single multi-AZ ASG's built-in AZ-rebalancing can transiently
        # deviate from a strict N-per-AZ count during scaling events),
        # min=max=desired=config.FIREWALL_APPLIANCES_PER_AZ (2), manually
        # wired to the GWLB target group via the L1 escape hatch since GWLB
        # target groups aren't one of the L2 AutoScalingGroup's built-in
        # attach_to_*_target_group helpers (those only cover ALB/NLB).
        # ------------------------------------------------------------------
        for i, fw_subnet in enumerate(firewall_subnets):
            asg = autoscaling.AutoScalingGroup(
                self, f"FirewallAsg{i}",
                vpc=vpc,
                launch_template=launch_template,
                vpc_subnets=ec2.SubnetSelection(subnets=[fw_subnet]),
                min_capacity=config.FIREWALL_APPLIANCES_PER_AZ,
                max_capacity=config.FIREWALL_APPLIANCES_PER_AZ,
                desired_capacity=config.FIREWALL_APPLIANCES_PER_AZ,
                # Generous grace period: boot includes an apt-get-based
                # gwlbtun/Suricata install plus a synchronous first pl-sync
                # run, not just a golden-AMI cold start (reference doc
                # Section 5.4 notes a golden AMI as the day-2 optimization
                # this project doesn't build yet).
                health_checks=autoscaling.HealthChecks.ec2(grace_period=Duration.minutes(10)),
            )
            cfn_asg = asg.node.default_child
            cfn_asg.target_group_arns = [target_group.ref]
            asg.node.add_dependency(target_group)

        # ------------------------------------------------------------------
        # Outputs
        # ------------------------------------------------------------------
        CfnOutput(self, "GwlbArn", value=gwlb.attr_load_balancer_arn)
        CfnOutput(self, "FirewallTargetGroupArn", value=target_group.ref)
        CfnOutput(self, "PolicyBucketName", value=policy_bucket.bucket_name)
        CfnOutput(self, "PlVersionParamName", value=pl_version_param.parameter_name)

        # ------------------------------------------------------------------
        # cdk-nag suppressions (SPEC.md Section 10)
        # ------------------------------------------------------------------
        NagSuppressions.add_stack_suppressions(self, [
            NagPackSuppression(
                id="AwsSolutions-IAM4",
                reason="AmazonSSMManagedInstanceCore / CloudWatchAgentServerPolicy are AWS-curated "
                       "policies appropriate for this lab's SSM access and log-shipping needs.",
            ),
            NagPackSuppression(
                id="AwsSolutions-IAM5",
                reason="Wildcards come from grant_read() (prefix-scoped to the policy bucket's own "
                       "prefix, not bucket-wide) and from AwsCustomResource SDK calls "
                       "(describeAutoScalingGroups, describeManagedPrefixLists) that have no "
                       "resource-level IAM support -- '*' is the only valid Resource for these actions.",
            ),
            NagPackSuppression(
                id="AwsSolutions-AS3",
                reason="ASG notifications need an SNS topic + subscription -- extra infrastructure not "
                       "core to this lab; CloudWatch dashboard (observability_stack.py) covers fleet "
                       "health instead.",
            ),
            NagPackSuppression(
                id="AwsSolutions-L1",
                reason="The flagged Lambda is AwsCustomResource's own framework-managed provider "
                       "function (used for the SSM/ASG lookups above), not project-authored code -- its "
                       "runtime version is controlled by the CDK library, not this project.",
            ),
            NagPackSuppression(
                id="AwsSolutions-S1",
                reason="Server access logging on the policy bucket would need a second bucket to receive "
                       "logs, purely for a lab artifact bucket holding non-sensitive nftables policy "
                       "YAML -- not worth the added infrastructure here.",
            ),
            NagPackSuppression(
                id="AwsSolutions-ELB2",
                reason="cdk-nag's access-log check errors out on this Gateway Load Balancer (GWLB is a "
                       "distinct ELBv2 type from ALB/NLB and isn't fully modeled by this check) -- "
                       "access logging isn't applicable to GWLB the way it is to ALB/NLB in any case.",
            ),
        ])
        NagSuppressions.add_resource_suppressions(
            firewall_sg,
            [NagPackSuppression(
                id="AwsSolutions-EC23",
                reason="cdk-nag can't statically resolve vpc.vpc_cidr_block (a CloudFormation token) to "
                       "confirm it isn't 0.0.0.0/0 -- it's inspection-vpc's own CIDR, not open access; "
                       "GENEVE/6081 and the health-check port are both scoped to that CIDR only.",
            )],
        )
