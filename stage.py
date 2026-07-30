"""Top-level Stage assembling every stack in dependency order (SPEC.md Section 3).

`app.py` instantiates this stage directly for local iteration (`cdk synth`,
`cdk deploy --all`); the self-mutating CDK Pipeline added in SPEC.md Section 7
will wrap this same stage for the git-driven deploy path -- no changes needed
here when that lands.

Shared objects (VPCs, Transit Gateway, service network, etc.) will be passed
between stacks as constructor props as each stack grows real resources, per
SPEC.md Section 3 ("not by re-lookup") -- there's nothing to wire yet since
every stack below is still an empty skeleton.
"""

from aws_cdk import Stage
from constructs import Construct

import config
from stacks.diagram_stack import DiagramStack
from stacks.inspection_stack import InspectionStack
from stacks.kafka_stack import KafkaStack
from stacks.lattice_stack import LatticeStack
from stacks.network_stack import NetworkStack
from stacks.observability_stack import ObservabilityStack
from stacks.privatelink_stack import PrivateLinkStack
from stacks.resource_groups_stack import ResourceGroupsStack
from stacks.threetier_stack import ThreeTierStack


class LandingZoneStage(Stage):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # Dependency order per SPEC.md Section 3 / Section 6. Shared objects
        # are passed as constructor props (SPEC.md Section 3: "not by
        # re-lookup") -- InspectionStack needs NetworkStack's VPCs/TGW to
        # build the GWLB endpoints, IGW ingress routing, and firewall fleet.
        network = NetworkStack(self, "NetworkStack")
        inspection = InspectionStack(self, "InspectionStack", network=network)
        privatelink = PrivateLinkStack(self, "PrivateLinkStack", network=network)
        threetier = ThreeTierStack(self, "ThreeTierStack", network=network)
        # Explicit ordering, not a functional dependency -- both stacks add a
        # small EC2 instance to app-vpc/provider-vpc's shared account-level
        # vCPU quota (LatticeInstanceTargetHost, ProviderInstance during any
        # future EC2 rollback), and deploying them in the same parallel wave
        # repeatedly raced: whichever stack was mid-replacement (freeing
        # vCPU) hadn't finished before the other tried to claim it, hitting
        # "requested more vCPU capacity than your current vCPU limit"
        # even with headroom that would exist a few seconds later. Forcing
        # PrivateLinkStack to finish first removes the race outright instead
        # of relying on retry timing.
        threetier.add_dependency(privatelink)
        lattice = LatticeStack(self, "LatticeStack", network=network, threetier=threetier)
        ObservabilityStack(self, "ObservabilityStack", inspection=inspection, threetier=threetier, lattice=lattice)
        ResourceGroupsStack(self, "ResourceGroupsStack")
        DiagramStack(self, "DiagramStack")

        if config.ENABLE_KAFKA:
            KafkaStack(self, "KafkaStack")
