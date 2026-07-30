"""kafka_stack.py -- real KRaft Kafka on EC2, deployed last, feature-flagged off first.

See SPEC.md Section 6 ("kafka_stack.py (deploy LAST, flag ENABLE_KAFKA=False
at first)"). Whether this stack is instantiated at all is gated by
config.ENABLE_KAFKA in stage.py -- this class itself has no internal flag
check.
"""

from aws_cdk import Stack, Tags
from constructs import Construct

import config


class KafkaStack(Stack):
    """Self-managed Kafka in KRaft mode (no ZooKeeper) on EC2 in onprem-vpc."""

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)
        Tags.of(self).add("Layer", config.Layer.KAFKA)

        # TODO (SPEC.md Section 6):
        #   - EC2 in onprem-vpc; user-data installs Kafka, formats storage with
        #     kafka-storage.sh, starts broker on 9092, advertised listener =
        #     its private IP.
        #   - Repoint LatticeStack's L4e resource configuration at this broker
        #     (replacing NetworkStack's placeholder ncat listener).
        #   - Producer/consumer test reachable from the app-vpc consumer
        #     through the Lattice mesh.
