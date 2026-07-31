"""network_stack.py -- VPCs + Transit Gateway + route tables + VPN + on-prem.

See SPEC.md Section 4 and docs/inspection-architecture-reference.md Section 4
(TGW appliance-mode / route-table symmetry requirements apply here too, even
though this project's topology -- NAT Gateway in app-vpc rather than
inspection-vpc -- differs from that reference's illustrative examples).

Topology recap:
  - Four "spokes" from the Transit Gateway's point of view: app-vpc,
    provider-vpc, inspection-vpc, and on-prem (reached via the Site-to-Site
    VPN's own auto-created TGW attachment -- onprem-vpc itself does NOT get a
    plain VPC attachment, so the VPN is the only path in/out, same as a real
    hybrid network).
  - spoke-rt (associated to app/provider/vpn attachments): a single static
    default route to the inspection VPC attachment -- forces all
    north-south *and* inter-spoke traffic through inspection first.
  - inspection-rt (associated to the inspection VPC attachment): propagated
    routes back to every spoke, plus a static default route to app-vpc's
    attachment specifically, since app-vpc is the only spoke with internet
    egress (its NAT Gateway) -- this is what sends inspected north-south
    traffic back out to the internet after inspection.
"""

import secrets

from aws_cdk import CfnOutput, Stack, Tags
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_iam as iam
from aws_cdk import custom_resources as cr
from cdk_nag import NagPackSuppression, NagSuppressions
from constructs import Construct

import config
from stacks.security_stack import SecurityStack


def _generate_psk() -> str:
    """AWS IPsec PSK constraints: 8-64 chars, alphanumeric/period/underscore, can't start with '0'."""
    return "k" + secrets.token_hex(24)


def _encrypted_root_volume(kms_key, size_gb: int = 8) -> list:
    """cdk-nag AwsSolutions-EC26 -- encrypt every instance's root EBS volume
    with security_stack.py's project-wide ebs CMK rather than the AWS-managed
    default key."""
    return [ec2.BlockDevice(device_name="/dev/xvda", volume=ec2.BlockDeviceVolume.ebs(size_gb, encrypted=True, kms_key=kms_key))]


class NetworkStack(Stack):
    """Four VPCs (onprem/inspection/app/provider), Transit Gateway, VPN, placeholder broker."""

    def __init__(self, scope: Construct, construct_id: str, *, security: SecurityStack, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)
        Tags.of(self).add("Layer", config.Layer.NETWORK)

        az_count = 2 if config.MULTI_AZ else 1
        # app-vpc gets >= 2 AZs unconditionally -- originally a hard
        # requirement (RDS's DBSubnetGroup needs >= 2 AZ coverage even for a
        # single-AZ instance). threetier_stack.py's data tier is DynamoDB
        # now, which has no such constraint, but this is left at 2 AZs
        # anyway: subnets themselves are free (only compute placed in them
        # costs money, so this doesn't undercut MULTI_AZ's cost intent), and
        # reverting risks destabilizing NetworkStack/InspectionStack/
        # PrivateLinkStack, which all already reference these exact
        # subnets and are stable as of this change.
        app_az_count = max(az_count, 2)

        # ------------------------------------------------------------------
        # VPCs
        # ------------------------------------------------------------------
        # app-vpc is the only VPC with a NAT Gateway (SPEC.md Section 4: "one
        # NAT gateway total ... in app-vpc"). It gets three subnet tiers so
        # "everything routes through inspection first" and "the NAT gateway
        # lives here" don't collide in a single route table:
        #   Public    -- NAT Gateway + IGW.
        #   TgwAttach -- hosts the TGW attachment ENI; default route -> NAT
        #                Gateway (inspected north-south traffic lands back
        #                here from the inspection VPC before actual egress).
        #   Private   -- compute; default route -> TGW attachment (so all
        #                egress / inter-spoke traffic is inspected first).
        # nat_gateways=0 here deliberately -- the NAT Gateway is created by
        # hand below so this stack controls exactly which subnet it sits in
        # and can reference its id for the TgwAttach subnet's custom route.
        self.app_vpc = ec2.Vpc(
            self, "AppVpc",
            vpc_name="app-vpc",
            ip_addresses=ec2.IpAddresses.cidr(config.VPC_CIDRS["app"]),
            max_azs=app_az_count,
            nat_gateways=0,
            flow_logs={"ToCloudWatch": ec2.FlowLogOptions()},
            subnet_configuration=[
                ec2.SubnetConfiguration(name="Public", subnet_type=ec2.SubnetType.PUBLIC, cidr_mask=24),
                ec2.SubnetConfiguration(name="TgwAttach", subnet_type=ec2.SubnetType.PRIVATE_ISOLATED, cidr_mask=28),
                ec2.SubnetConfiguration(name="Private", subnet_type=ec2.SubnetType.PRIVATE_ISOLATED, cidr_mask=24),
            ],
        )

        # inspection-vpc needs FOUR subnet tiers, not two -- same reason
        # app-vpc needed three instead of one: GWLB inspection traffic and
        # the firewall fleet's own management-plane traffic must not share a
        # route table, or "send inspected traffic onward" and "send my own
        # apt-get traffic out" collide on the same 0.0.0.0/0 destination.
        #   Public       -- IGW + a small dedicated NAT INSTANCE (not a
        #                    managed NAT Gateway -- see below) used ONLY by
        #                    the Firewall subnet, kept deliberately separate
        #                    from app-vpc's shared NAT/IGW-redirect path.
        #   TgwAttach    -- hosts the TGW attachment ENI; default route ->
        #                    GWLB Endpoint (same AZ) -- added by
        #                    inspection_stack.py once the endpoint exists.
        #   GwlbEndpoint -- hosts the GWLB Endpoint; default route -> TGW
        #                    attachment (added below, right after the TGW
        #                    attachment -- this one has no GWLB dependency).
        #   Firewall     -- hosts the firewall EC2 instances (GWLB targets);
        #                    default route -> the Public subnet's NAT
        #                    instance (management-plane egress only).
        #
        # Why a dedicated NAT instance here instead of routing the firewall
        # fleet's own egress through app-vpc's shared NAT (which would avoid
        # a second NAT resource entirely): app-vpc's NAT sits behind an IGW
        # ingress edge-association route table (inspection_stack.py) that
        # blanket-redirects ALL return traffic for that NAT's EIP back
        # through a GWLB endpoint for inspection. The firewall fleet's own
        # outbound package-manager traffic (source = inspection-vpc's own
        # CIDR, which is neither trust nor dmz, so it's classified as
        # untrust) would come back through that same redirect and get
        # re-inspected by nftables' untrust->untrust path -- which has no
        # rule and is default-deny, silently breaking the firewall's own
        # ability to update itself. A separate, un-inspected NAT instance
        # sidesteps that recursion entirely. It's an EC2 instance, not a
        # managed NAT Gateway, specifically to keep this narrowly-scoped
        # exception cheap relative to SPEC.md Section 1's "one NAT gateway
        # total" cost intent for the DATA-plane design in this file.
        self.inspection_vpc = ec2.Vpc(
            self, "InspectionVpc",
            vpc_name="inspection-vpc",
            ip_addresses=ec2.IpAddresses.cidr(config.VPC_CIDRS["inspection"]),
            max_azs=config.INSPECTION_AZ_COUNT,
            nat_gateways=0,
            flow_logs={"ToCloudWatch": ec2.FlowLogOptions()},
            subnet_configuration=[
                ec2.SubnetConfiguration(name="Public", subnet_type=ec2.SubnetType.PUBLIC, cidr_mask=24),
                ec2.SubnetConfiguration(name="TgwAttach", subnet_type=ec2.SubnetType.PRIVATE_ISOLATED, cidr_mask=28),
                ec2.SubnetConfiguration(name="GwlbEndpoint", subnet_type=ec2.SubnetType.PRIVATE_ISOLATED, cidr_mask=28),
                ec2.SubnetConfiguration(name="Firewall", subnet_type=ec2.SubnetType.PRIVATE_ISOLATED, cidr_mask=24),
            ],
        )

        self.provider_vpc = ec2.Vpc(
            self, "ProviderVpc",
            vpc_name="provider-vpc",
            ip_addresses=ec2.IpAddresses.cidr(config.VPC_CIDRS["provider"]),
            max_azs=az_count,
            nat_gateways=0,
            flow_logs={"ToCloudWatch": ec2.FlowLogOptions()},
            subnet_configuration=[
                ec2.SubnetConfiguration(name="Public", subnet_type=ec2.SubnetType.PUBLIC, cidr_mask=24),
                ec2.SubnetConfiguration(name="Private", subnet_type=ec2.SubnetType.PRIVATE_ISOLATED, cidr_mask=24),
            ],
        )

        # onprem-vpc simulates the datacenter. It deliberately does NOT get a
        # Transit Gateway VPC attachment -- the Site-to-Site VPN (below) is
        # the *only* path in/out, exactly like a real hybrid network. Its
        # Public subnet hosts the libreswan "Customer Gateway" EC2; its
        # Private subnet hosts the placeholder broker.
        self.onprem_vpc = ec2.Vpc(
            self, "OnpremVpc",
            vpc_name="onprem-vpc",
            ip_addresses=ec2.IpAddresses.cidr(config.VPC_CIDRS["onprem"]),
            max_azs=az_count,
            nat_gateways=0,
            flow_logs={"ToCloudWatch": ec2.FlowLogOptions()},
            subnet_configuration=[
                ec2.SubnetConfiguration(name="Public", subnet_type=ec2.SubnetType.PUBLIC, cidr_mask=24),
                ec2.SubnetConfiguration(name="Private", subnet_type=ec2.SubnetType.PRIVATE_ISOLATED, cidr_mask=24),
            ],
        )

        # ------------------------------------------------------------------
        # app-vpc's single NAT Gateway (hand-built -- see comment above)
        # ------------------------------------------------------------------
        nat_eip = ec2.CfnEIP(self, "AppNatEip", domain="vpc")
        app_public_subnet_ids = self.app_vpc.select_subnets(subnet_group_name="Public").subnet_ids
        nat_gateway = ec2.CfnNatGateway(
            self, "AppNatGateway",
            subnet_id=app_public_subnet_ids[0],
            allocation_id=nat_eip.attr_allocation_id,
        )

        for subnet in self.app_vpc.select_subnets(subnet_group_name="TgwAttach").subnets:
            subnet.add_route(
                "DefaultToNat",
                router_type=ec2.RouterType.NAT_GATEWAY,
                router_id=nat_gateway.ref,
                destination_cidr_block="0.0.0.0/0",
            )

        # ------------------------------------------------------------------
        # Transit Gateway -- explicit routing only (SPEC.md Section 4)
        # ------------------------------------------------------------------
        self.tgw = ec2.CfnTransitGateway(
            self, "Tgw",
            description="lattice-lab transit gateway",
            default_route_table_association="disable",
            default_route_table_propagation="disable",
        )

        spoke_rt = ec2.CfnTransitGatewayRouteTable(self, "SpokeRouteTable", transit_gateway_id=self.tgw.ref)
        inspection_rt = ec2.CfnTransitGatewayRouteTable(self, "InspectionRouteTable", transit_gateway_id=self.tgw.ref)
        self.spoke_route_table = spoke_rt
        self.inspection_route_table = inspection_rt

        # ---- VPC attachments ----
        app_att = ec2.CfnTransitGatewayVpcAttachment(
            self, "AppVpcAttachment",
            transit_gateway_id=self.tgw.ref,
            vpc_id=self.app_vpc.vpc_id,
            subnet_ids=self.app_vpc.select_subnets(subnet_group_name="TgwAttach").subnet_ids,
        )
        provider_att = ec2.CfnTransitGatewayVpcAttachment(
            self, "ProviderVpcAttachment",
            transit_gateway_id=self.tgw.ref,
            vpc_id=self.provider_vpc.vpc_id,
            subnet_ids=self.provider_vpc.select_subnets(subnet_group_name="Private").subnet_ids,
        )
        # Mandatory for flow symmetry once inspection_stack.py's GWLB target
        # group also has Appliance Mode enabled -- see
        # docs/inspection-architecture-reference.md Section 4.2. These are
        # two INDEPENDENT switches; both must be on.
        inspection_att = ec2.CfnTransitGatewayVpcAttachment(
            self, "InspectionVpcAttachment",
            transit_gateway_id=self.tgw.ref,
            vpc_id=self.inspection_vpc.vpc_id,
            subnet_ids=self.inspection_vpc.select_subnets(subnet_group_name="TgwAttach").subnet_ids,
            options={"ApplianceModeSupport": "enable"},
        )
        self.inspection_attachment = inspection_att

        # app + provider land on spoke-rt; inspection lands on inspection-rt.
        for name, att in {"App": app_att, "Provider": provider_att}.items():
            ec2.CfnTransitGatewayRouteTableAssociation(
                self, f"{name}SpokeRtAssoc",
                transit_gateway_attachment_id=att.ref,
                transit_gateway_route_table_id=spoke_rt.ref,
            )
            ec2.CfnTransitGatewayRouteTablePropagation(
                self, f"{name}PropagateToInspectionRt",
                transit_gateway_attachment_id=att.ref,
                transit_gateway_route_table_id=inspection_rt.ref,
            )
        ec2.CfnTransitGatewayRouteTableAssociation(
            self, "InspectionRtAssoc",
            transit_gateway_attachment_id=inspection_att.ref,
            transit_gateway_route_table_id=inspection_rt.ref,
        )

        # spoke-rt: ONE static default route to inspection. No propagation of
        # spoke CIDRs into this table -- spokes must not be able to see each
        # other directly, or inter-spoke traffic could bypass inspection.
        ec2.CfnTransitGatewayRoute(
            self, "SpokeDefaultToInspection",
            transit_gateway_route_table_id=spoke_rt.ref,
            destination_cidr_block="0.0.0.0/0",
            transit_gateway_attachment_id=inspection_att.ref,
        )
        # inspection-rt: static default route back to app-vpc specifically,
        # since app-vpc is the only spoke with internet egress (its NAT
        # Gateway) -- this is what sends inspected north-south traffic back
        # out after inspection. Propagated spoke routes (wired above/below)
        # handle the east-west return legs.
        ec2.CfnTransitGatewayRoute(
            self, "InspectionDefaultToApp",
            transit_gateway_route_table_id=inspection_rt.ref,
            destination_cidr_block="0.0.0.0/0",
            transit_gateway_attachment_id=app_att.ref,
        )

        # app-vpc's Private (compute) subnets: send everything to inspection first.
        # Dependency is scoped to the created CfnRoute itself (via
        # node.find_child -- add_route() returns None, not the resource),
        # not subnet.node.add_dependency(app_att): that would put the
        # dependency on the whole subnet construct, including the Subnet
        # resource app_att's own subnet_ids already depends on. Harmless
        # here (app_att's attachment sits in the separate TgwAttach tier),
        # but provider-vpc's identical pattern below hits exactly that
        # 2-node cycle since its attachment and this route share one tier.
        for subnet in self.app_vpc.select_subnets(subnet_group_name="Private").subnets:
            subnet.add_route(
                "DefaultToTgw",
                router_type=ec2.RouterType.TRANSIT_GATEWAY,
                router_id=self.tgw.ref,
                destination_cidr_block="0.0.0.0/0",
            )
            subnet.node.find_child("DefaultToTgw").node.add_dependency(app_att)

        # provider-vpc's Private subnets: same pattern, everything via TGW.
        # provider_att's subnet_ids ARE these same Private subnets (provider-vpc
        # only has Public/Private tiers, no dedicated TgwAttach tier like
        # app-vpc/inspection-vpc) -- subnet.node.add_dependency(provider_att)
        # would make the Subnet resource depend on the Attachment that itself
        # depends on that Subnet's ref, a direct CloudFormation circular
        # dependency. Scoping to just the CfnRoute avoids it.
        for subnet in self.provider_vpc.select_subnets(subnet_group_name="Private").subnets:
            subnet.add_route(
                "DefaultToTgw",
                router_type=ec2.RouterType.TRANSIT_GATEWAY,
                router_id=self.tgw.ref,
                destination_cidr_block="0.0.0.0/0",
            )
            subnet.node.find_child("DefaultToTgw").node.add_dependency(provider_att)

        # inspection-vpc's GwlbEndpoint subnets: after GWLB hands inspected
        # traffic back, it needs to go back out via TGW (no local NAT here --
        # this project's NAT lives in app-vpc). No GWLB dependency for THIS
        # route (unlike the TgwAttach subnet's route, added by
        # inspection_stack.py once the GWLB Endpoint itself exists) -- it's
        # plain TGW routing, so it belongs here alongside the rest of the
        # TGW wiring.
        for subnet in self.inspection_vpc.select_subnets(subnet_group_name="GwlbEndpoint").subnets:
            subnet.add_route(
                "DefaultToTgw",
                router_type=ec2.RouterType.TRANSIT_GATEWAY,
                router_id=self.tgw.ref,
                destination_cidr_block="0.0.0.0/0",
            )
            subnet.node.find_child("DefaultToTgw").node.add_dependency(inspection_att)

        # ------------------------------------------------------------------
        # inspection-vpc's dedicated NAT instance -- firewall fleet's own
        # management-plane egress (package installs, rule updates), kept
        # separate from the GWLB data path -- see the long comment on
        # inspection_vpc's subnet_configuration above for why.
        # ------------------------------------------------------------------
        inspection_nat_sg = ec2.SecurityGroup(
            self, "InspectionNatSg",
            vpc=self.inspection_vpc,
            description="inspection-vpc NAT instance -- inbound from Firewall subnet only",
            allow_all_outbound=True,
        )
        inspection_nat_sg.add_ingress_rule(
            ec2.Peer.ipv4(config.VPC_CIDRS["inspection"]), ec2.Port.all_traffic(), "inspection-vpc CIDR"
        )

        inspection_nat_role = iam.Role(
            self, "InspectionNatRole",
            assumed_by=iam.ServicePrincipal("ec2.amazonaws.com"),
            permissions_boundary=security.permissions_boundary,
            managed_policies=[iam.ManagedPolicy.from_aws_managed_policy_name("AmazonSSMManagedInstanceCore")],
        )

        inspection_nat = ec2.Instance(
            self, "InspectionNatInstance",
            vpc=self.inspection_vpc,
            vpc_subnets=ec2.SubnetSelection(subnet_group_name="Public"),
            instance_type=ec2.InstanceType(config.DEFAULT_INSTANCE_TYPE),
            machine_image=ec2.MachineImage.latest_amazon_linux2023(),
            security_group=inspection_nat_sg,
            role=inspection_nat_role,
            source_dest_check=False,
            ssm_session_permissions=True,
            block_devices=_encrypted_root_volume(security.ebs_key),
        )
        inspection_nat.user_data.add_commands(
            "set -e",
            "sysctl -w net.ipv4.ip_forward=1",
            "echo 'net.ipv4.ip_forward = 1' > /etc/sysctl.d/99-inspection-nat.conf",
            "OUT_IF=$(ip route show default | awk '{print $5; exit}')",
            'iptables -t nat -A POSTROUTING -o "$OUT_IF" -j MASQUERADE',
            "mkdir -p /etc/systemd/system/iptables-restore.service.d",
            "iptables-save > /etc/inspection-nat.rules",
            "cat > /etc/systemd/system/inspection-nat-rules.service << 'EOF'\n"
            "[Unit]\n"
            "Description=Restore inspection-vpc NAT instance iptables rules\n"
            "After=network.target\n"
            "[Service]\n"
            "Type=oneshot\n"
            "ExecStart=/usr/sbin/iptables-restore /etc/inspection-nat.rules\n"
            "RemainAfterExit=true\n"
            "[Install]\n"
            "WantedBy=multi-user.target\n"
            "EOF",
            "systemctl daemon-reload",
            "systemctl enable inspection-nat-rules",
        )

        for subnet in self.inspection_vpc.select_subnets(subnet_group_name="Firewall").subnets:
            subnet.add_route(
                "DefaultToNatInstance",
                router_type=ec2.RouterType.INSTANCE,
                router_id=inspection_nat.instance_id,
                destination_cidr_block="0.0.0.0/0",
            )

        self.inspection_nat_instance = inspection_nat

        # NOTE: the "temporary app-vpc test host" this section originally had
        # (SPEC.md Section 4's "from an SSM session on the app-vpc test host,
        # nc -vz ..." verify step) was removed -- threetier_stack.py's own
        # LatticeInstanceTargetHost (a small dedicated EC2 instance, kept
        # around anyway since VPC Lattice's INSTANCE-type target group needs
        # a real instance id -- see that stack's module docstring) now
        # serves this role too. The account's default EC2 vCPU quota (16,
        # Standard A/C/D/H/I/M/R/T/Z family) left no headroom for a
        # dedicated test host once the firewall fleet + NAT/libreswan/broker
        # instances were running, so this was removed rather than kept
        # alongside it. Use an SSM session on that instance for the verify
        # steps instead.

        # ------------------------------------------------------------------
        # Site-to-Site VPN as the Direct Connect stand-in (SPEC.md Section 4)
        # ------------------------------------------------------------------
        # libreswan EC2 in onprem-vpc's Public subnet plays the on-prem
        # Customer Gateway device. It needs its own public (Elastic) IP --
        # AWS's VPN service must be able to reach it -- and it routes /
        # NATs traffic for the rest of onprem-vpc, so (unlike the GWLB
        # firewall appliances elsewhere in this project) disabling
        # source_dest_check on THIS instance is the correct, legitimate use
        # of that setting: it's a classic NAT-instance/router pattern, not a
        # GWLB-fronted appliance.
        onprem_private_subnets = self.onprem_vpc.select_subnets(subnet_group_name="Private").subnets

        libreswan_sg = ec2.SecurityGroup(
            self, "LibreswanSg",
            vpc=self.onprem_vpc,
            description="libreswan Customer Gateway -- IKE/NAT-T from AWS VPN endpoints",
            allow_all_outbound=True,
        )
        libreswan_sg.add_ingress_rule(ec2.Peer.any_ipv4(), ec2.Port.udp(500), "IKE")
        libreswan_sg.add_ingress_rule(ec2.Peer.any_ipv4(), ec2.Port.udp(4500), "IKE NAT-T")

        psk_1 = _generate_psk()
        psk_2 = _generate_psk()

        libreswan_role = iam.Role(
            self, "LibreswanRole",
            assumed_by=iam.ServicePrincipal("ec2.amazonaws.com"),
            permissions_boundary=security.permissions_boundary,
            managed_policies=[iam.ManagedPolicy.from_aws_managed_policy_name("AmazonSSMManagedInstanceCore")],
        )
        # describe-vpn-connections/describe-customer-gateways have no
        # resource-level IAM support; the instance uses these at boot to
        # discover its own VPN connection and tunnels' outside IPs -- it
        # can't be told these via UserData (see the CfnEIP/UserData comment
        # below for why), so it looks them up for itself instead.
        libreswan_role.add_to_policy(iam.PolicyStatement(
            actions=["ec2:DescribeVpnConnections", "ec2:DescribeCustomerGateways"], resources=["*"]
        ))

        libreswan = ec2.Instance(
            self, "LibreswanCgw",
            vpc=self.onprem_vpc,
            vpc_subnets=ec2.SubnetSelection(subnet_group_name="Public"),
            instance_type=ec2.InstanceType(config.DEFAULT_INSTANCE_TYPE),
            machine_image=ec2.MachineImage.latest_amazon_linux2023(),
            security_group=libreswan_sg,
            role=libreswan_role,
            source_dest_check=False,
            ssm_session_permissions=True,
            block_devices=_encrypted_root_volume(security.ebs_key),
        )

        # This EIP's InstanceId makes it depend on libreswan (the instance
        # must exist first) -- so libreswan's own UserData (baked in at
        # instance CREATE time) can NEVER reference this EIP's address, or
        # any resource that itself depends on this EIP (OnpremCustomerGateway,
        # OnpremVpnConnection, ...), without creating a CloudFormation
        # circular dependency. UserData below discovers its own public IP
        # and the resulting VPN connection ID at boot time (instance
        # metadata + API filters) instead of taking them as CFN tokens.
        libreswan_eip = ec2.CfnEIP(self, "LibreswanEip", domain="vpc", instance_id=libreswan.instance_id)

        customer_gateway = ec2.CfnCustomerGateway(
            self, "OnpremCustomerGateway",
            type="ipsec.1",
            bgp_asn=65000,
            ip_address=libreswan_eip.attr_public_ip,
        )

        vpn_connection = ec2.CfnVPNConnection(
            self, "OnpremVpnConnection",
            type="ipsec.1",
            customer_gateway_id=customer_gateway.attr_customer_gateway_id,
            transit_gateway_id=self.tgw.ref,
            static_routes_only=True,
            vpn_tunnel_options_specifications=[
                ec2.CfnVPNConnection.VpnTunnelOptionsSpecificationProperty(pre_shared_key=psk_1),
                ec2.CfnVPNConnection.VpnTunnelOptionsSpecificationProperty(pre_shared_key=psk_2),
            ],
        )
        vpn_connection.node.add_dependency(customer_gateway)

        # NOT ec2.CfnVPNConnectionRoute here -- that's AWS::EC2::VPNConnectionRoute,
        # valid only for VPN connections attached to a Virtual Private Gateway.
        # This one has transit_gateway_id set instead, and AWS rejects
        # VPNConnectionRoute against a TGW-attached VPN at deploy time
        # ("Static routes for vpn-xxx must be added through the Transit
        # Gateway API") -- the onprem CIDR's static route is added below, as
        # a CfnTransitGatewayRoute in inspection_rt, once the VPN's TGW
        # attachment id is known.

        # CloudFormation doesn't expose the VPN connection's auto-created TGW
        # attachment id as a GetAtt -- look it up with a scoped Custom
        # Resource so it can be associated/propagated like any other
        # attachment (default association/propagation is disabled at the TGW
        # level, so this attachment needs the same explicit wiring too).
        vpn_attachment_lookup = cr.AwsCustomResource(
            self, "VpnTgwAttachmentLookup",
            on_create=cr.AwsSdkCall(
                service="EC2",
                action="describeTransitGatewayAttachments",
                parameters={
                    "Filters": [
                        {"Name": "resource-id", "Values": [vpn_connection.attr_vpn_connection_id]},
                        {"Name": "resource-type", "Values": ["vpn"]},
                    ]
                },
                physical_resource_id=cr.PhysicalResourceId.of("OnpremVpnTgwAttachmentLookup"),
            ),
            policy=cr.AwsCustomResourcePolicy.from_sdk_calls(resources=cr.AwsCustomResourcePolicy.ANY_RESOURCE),
        )
        vpn_attachment_lookup.node.add_dependency(vpn_connection)
        vpn_tgw_attachment_id = vpn_attachment_lookup.get_response_field(
            "TransitGatewayAttachments.0.TransitGatewayAttachmentId"
        )

        ec2.CfnTransitGatewayRouteTableAssociation(
            self, "OnpremVpnSpokeRtAssoc",
            transit_gateway_attachment_id=vpn_tgw_attachment_id,
            transit_gateway_route_table_id=spoke_rt.ref,
        )
        ec2.CfnTransitGatewayRouteTablePropagation(
            self, "OnpremVpnPropagateToInspectionRt",
            transit_gateway_attachment_id=vpn_tgw_attachment_id,
            transit_gateway_route_table_id=inspection_rt.ref,
        )
        # The onprem CIDR's static route (see the "NOT ec2.CfnVPNConnectionRoute"
        # comment above) -- goes in inspection_rt specifically, matching every
        # other spoke: north-south/east-west traffic already lands in
        # inspection-vpc via spoke_rt's single default route, and this is
        # what lets inspected traffic find its way back out to onprem.
        # Propagation alone (above) wouldn't add this: static_routes_only=True
        # means there's no BGP session to propagate FROM.
        ec2.CfnTransitGatewayRoute(
            self, "InspectionToOnpremViaVpn",
            transit_gateway_route_table_id=inspection_rt.ref,
            destination_cidr_block=config.VPC_CIDRS["onprem"],
            transit_gateway_attachment_id=vpn_tgw_attachment_id,
        )

        # onprem-vpc's Private subnets (the broker's subnet) route AWS-side
        # CIDRs through libreswan -- 10.0.0.0/14 covers inspection (10.0/16),
        # app (10.1/16), and provider (10.2/16) with one summarized route.
        for subnet in onprem_private_subnets:
            subnet.add_route(
                "ToAwsViaLibreswan",
                router_type=ec2.RouterType.INSTANCE,
                router_id=libreswan.instance_id,
                destination_cidr_block="10.0.0.0/14",
            )

        # libreswan needs IP forwarding; the tunnel-mode IPsec policy below
        # (leftsubnet/rightsubnet) handles encryption without needing a
        # virtual tunnel interface.
        region = self.region
        libreswan.user_data.add_commands(
            "set -e",
            "dnf install -y libreswan awscli",
            "sysctl -w net.ipv4.ip_forward=1",
            "echo 'net.ipv4.ip_forward = 1' > /etc/sysctl.d/99-onprem-router.conf",
            f"REGION={region}",
            # Own public IP (== this EIP once associated) and the resulting
            # VPN connection id, both discovered at boot rather than passed
            # in as CFN tokens -- see the CfnEIP comment above.
            "TOKEN=$(curl -s -X PUT \"http://169.254.169.254/latest/api/token\" "
            "-H \"X-aws-ec2-metadata-token-ttl-seconds: 21600\")",
            "for i in 1 2 3 4 5 6 7 8 9 10; do "
            "MY_PUBLIC_IP=$(curl -s -f -H \"X-aws-ec2-metadata-token: $TOKEN\" "
            "http://169.254.169.254/latest/meta-data/public-ipv4) "
            "&& [ -n \"$MY_PUBLIC_IP\" ] && break || sleep 15; done",
            "for i in 1 2 3 4 5 6 7 8 9 10; do "
            "CGW_ID=$(aws ec2 describe-customer-gateways --filters \"Name=ip-address,Values=$MY_PUBLIC_IP\" "
            "\"Name=state,Values=available\" --region \"$REGION\" "
            "--query 'CustomerGateways[0].CustomerGatewayId' --output text) "
            "&& [ \"$CGW_ID\" != \"None\" ] && [ -n \"$CGW_ID\" ] && break || sleep 15; done",
            "for i in 1 2 3 4 5 6 7 8 9 10; do "
            "VPN_CONNECTION_ID=$(aws ec2 describe-vpn-connections "
            "--filters \"Name=customer-gateway-id,Values=$CGW_ID\" --region \"$REGION\" "
            "--query 'VpnConnections[0].VpnConnectionId' --output text) "
            "&& [ \"$VPN_CONNECTION_ID\" != \"None\" ] && [ -n \"$VPN_CONNECTION_ID\" ] && break || sleep 15; done",
            "for i in 1 2 3 4 5 6 7 8 9 10; do "
            "TUNNEL_INFO=$(aws ec2 describe-vpn-connections --vpn-connection-ids \"$VPN_CONNECTION_ID\" "
            "--region \"$REGION\" --output json) && break || sleep 15; done",
            "OUTSIDE_IP_1=$(echo \"$TUNNEL_INFO\" | python3 -c "
            "\"import json,sys; d=json.load(sys.stdin); "
            "print(d['VpnConnections'][0]['VgwTelemetry'][0]['OutsideIpAddress'])\")",
            "OUTSIDE_IP_2=$(echo \"$TUNNEL_INFO\" | python3 -c "
            "\"import json,sys; d=json.load(sys.stdin); "
            "print(d['VpnConnections'][0]['VgwTelemetry'][1]['OutsideIpAddress'])\")",
            "cat > /etc/ipsec.d/aws-tunnel1.conf << EOF\n"
            "conn aws-tunnel1\n"
            "  authby=secret\n"
            "  auto=start\n"
            "  type=tunnel\n"
            "  ikev2=no\n"
            f"  left=%defaultroute\n"
            "  leftid=$MY_PUBLIC_IP\n"
            f"  leftsubnet={config.VPC_CIDRS['onprem']}\n"
            "  right=$OUTSIDE_IP_1\n"
            "  rightsubnet=0.0.0.0/0\n"
            "  ike=aes128-sha1;modp1024\n"
            "  phase2=esp\n"
            "  phase2alg=aes128-sha1;modp1024\n"
            "  ikelifetime=8h\n"
            "  salifetime=1h\n"
            "  keyingtries=%forever\n"
            "  dpddelay=10\n"
            "  dpdtimeout=30\n"
            "  dpdaction=restart_by_peer\n"
            "EOF",
            "cat > /etc/ipsec.d/aws-tunnel2.conf << EOF\n"
            "conn aws-tunnel2\n"
            "  authby=secret\n"
            "  auto=start\n"
            "  type=tunnel\n"
            "  ikev2=no\n"
            f"  left=%defaultroute\n"
            "  leftid=$MY_PUBLIC_IP\n"
            f"  leftsubnet={config.VPC_CIDRS['onprem']}\n"
            "  right=$OUTSIDE_IP_2\n"
            "  rightsubnet=0.0.0.0/0\n"
            "  ike=aes128-sha1;modp1024\n"
            "  phase2=esp\n"
            "  phase2alg=aes128-sha1;modp1024\n"
            "  ikelifetime=8h\n"
            "  salifetime=1h\n"
            "  keyingtries=%forever\n"
            "  dpddelay=10\n"
            "  dpdtimeout=30\n"
            "  dpdaction=restart_by_peer\n"
            "EOF",
            f"echo \"$MY_PUBLIC_IP $OUTSIDE_IP_1: PSK \\\"{psk_1}\\\"\" >> /etc/ipsec.secrets",
            f"echo \"$MY_PUBLIC_IP $OUTSIDE_IP_2: PSK \\\"{psk_2}\\\"\" >> /etc/ipsec.secrets",
            "chmod 600 /etc/ipsec.secrets",
            "systemctl enable ipsec",
            "systemctl restart ipsec",
        )

        # ------------------------------------------------------------------
        # Placeholder broker (swapped for real Kafka in kafka_stack.py later)
        # ------------------------------------------------------------------
        broker_sg = ec2.SecurityGroup(
            self, "BrokerSg",
            vpc=self.onprem_vpc,
            description="placeholder broker -- TCP 9092 from AWS-side lab CIDRs",
            allow_all_outbound=True,
        )
        broker_sg.add_ingress_rule(ec2.Peer.ipv4("10.0.0.0/8"), ec2.Port.tcp(9092), "broker (lab CIDRs)")

        broker_role = iam.Role(
            self, "BrokerRole",
            assumed_by=iam.ServicePrincipal("ec2.amazonaws.com"),
            permissions_boundary=security.permissions_boundary,
            managed_policies=[iam.ManagedPolicy.from_aws_managed_policy_name("AmazonSSMManagedInstanceCore")],
        )

        self.broker = ec2.Instance(
            self, "PlaceholderBroker",
            vpc=self.onprem_vpc,
            vpc_subnets=ec2.SubnetSelection(subnet_group_name="Private"),
            instance_type=ec2.InstanceType(config.DEFAULT_INSTANCE_TYPE),
            machine_image=ec2.MachineImage.latest_amazon_linux2023(),
            security_group=broker_sg,
            role=broker_role,
            ssm_session_permissions=True,
            block_devices=_encrypted_root_volume(security.ebs_key),
        )
        self.broker.user_data.add_commands(
            "set -e",
            "dnf install -y nmap-ncat",
            "cat > /etc/systemd/system/broker-placeholder.service << 'EOF'\n"
            "[Unit]\n"
            "Description=Placeholder TCP broker (swapped for real Kafka later)\n"
            "After=network.target\n"
            "[Service]\n"
            "ExecStart=/usr/bin/ncat -k -l 9092\n"
            "Restart=always\n"
            "[Install]\n"
            "WantedBy=multi-user.target\n"
            "EOF",
            "systemctl daemon-reload",
            "systemctl enable --now broker-placeholder",
        )

        # ------------------------------------------------------------------
        # Outputs -- for the SSM/nc verify step
        # ------------------------------------------------------------------
        CfnOutput(self, "TransitGatewayId", value=self.tgw.ref)
        CfnOutput(self, "VpnConnectionId", value=vpn_connection.attr_vpn_connection_id)
        CfnOutput(self, "LibreswanPublicIp", value=libreswan_eip.attr_public_ip)
        CfnOutput(self, "LibreswanInstanceId", value=libreswan.instance_id)
        CfnOutput(self, "BrokerPrivateIp", value=self.broker.instance_private_ip)
        CfnOutput(self, "BrokerInstanceId", value=self.broker.instance_id)

        # ------------------------------------------------------------------
        # cdk-nag suppressions (SPEC.md Section 10: "document any
        # suppressions"). Real fixes (EBS encryption, VPC Flow Logs) were
        # applied directly above, not suppressed -- only genuine lab
        # trade-offs or tool limitations are suppressed here.
        # ------------------------------------------------------------------
        NagSuppressions.add_stack_suppressions(self, [
            NagPackSuppression(
                id="AwsSolutions-IAM4",
                reason="AmazonSSMManagedInstanceCore is an AWS-curated policy appropriate for SSM-only "
                       "instance access in this lab; no custom policy adds real security value here.",
            ),
            NagPackSuppression(
                id="AwsSolutions-IAM5",
                reason="Wildcard resources come from two sources, both legitimate: (1) CDK's own "
                       "grant_read()-generated policies, which are prefix-scoped where the data is "
                       "sensitive (e.g. the inspection policy bucket) and only genuinely broad for "
                       "throwaway lab buckets; (2) AwsCustomResource calls to EC2/AutoScaling/ACM SDK "
                       "actions (describeVpnConnections, describeTransitGatewayAttachments, "
                       "importCertificate) that have no resource-level IAM support at all -- '*' is the "
                       "only valid Resource for these, not a scoping choice.",
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
        NagSuppressions.add_resource_suppressions(
            libreswan_sg,
            [NagPackSuppression(
                id="AwsSolutions-EC23",
                reason="Genuinely open by design, not an oversight: this SG fronts the Site-to-Site VPN "
                       "Customer Gateway (libreswan). AWS's VPN service endpoints don't have fixed, "
                       "predictable source IPs, so IKE (UDP/500) and NAT-T (UDP/4500) must accept from "
                       "0.0.0.0/0 -- exactly like a real internet-facing customer gateway appliance.",
            )],
        )
