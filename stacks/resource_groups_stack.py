"""resource_groups_stack.py -- tag-based AWS Resource Groups (SPEC.md Section 3.5).

One umbrella group matching Project=lattice-lab, plus one group per
config.ALL_LAYERS, so each layer's resources are visible together in the
console -- useful for teardown auditing and cost allocation, not just browsing.
"""

from aws_cdk import CfnOutput, Stack
from aws_cdk import aws_resourcegroups as rg
from constructs import Construct

import config


class ResourceGroupsStack(Stack):
    """CfnGroup (TAG_FILTERS_1_0) per config.ALL_LAYERS, plus one umbrella group."""

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)
        # Deliberately NOT tagged with this stack's own Layer -- a
        # tag-based query group that is itself part of the tag set it's
        # querying is confusing to reason about, and this stack has nothing
        # else that would benefit from its own layer grouping.

        umbrella = rg.CfnGroup(
            self, "UmbrellaGroup",
            name="lattice-lab-all",
            description=f"All resources tagged Project {config.PROJECT_TAG}",
            resource_query=rg.CfnGroup.ResourceQueryProperty(
                type="TAG_FILTERS_1_0",
                query=rg.CfnGroup.QueryProperty(
                    resource_type_filters=["AWS::AllSupported"],
                    tag_filters=[rg.CfnGroup.TagFilterProperty(key="Project", values=[config.PROJECT_TAG])],
                ),
            ),
        )
        CfnOutput(self, "UmbrellaGroupName", value=umbrella.name)

        for layer in config.ALL_LAYERS:
            group = rg.CfnGroup(
                self, f"{layer.capitalize()}LayerGroup",
                name=f"lattice-lab-{layer}",
                description=f"Project {config.PROJECT_TAG} Layer {layer}",
                resource_query=rg.CfnGroup.ResourceQueryProperty(
                    type="TAG_FILTERS_1_0",
                    query=rg.CfnGroup.QueryProperty(
                        resource_type_filters=["AWS::AllSupported"],
                        tag_filters=[
                            rg.CfnGroup.TagFilterProperty(key="Project", values=[config.PROJECT_TAG]),
                            rg.CfnGroup.TagFilterProperty(key="Layer", values=[layer]),
                        ],
                    ),
                ),
            )
            CfnOutput(self, f"{layer.capitalize()}LayerGroupName", value=group.name)
