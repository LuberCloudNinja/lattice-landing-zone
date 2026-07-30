"""pipeline_stack.py -- self-mutating CDK Pipeline sourced from GitHub (SPEC.md Section 7).

Deploy once by hand (`cdk deploy PipelineStack --profile deloitte`); every
push to config.GITHUB_BRANCH after that re-synths, self-mutates the pipeline
if this file itself changed, and deploys LandingZoneStage automatically --
gated behind the manual approval step below.

One-time manual step this code cannot do for you (SPEC.md Section 7): the
CodeConnections connection identified by config.CODECONNECTIONS_ARN must
already exist and be authorized with GitHub via the console
(CodePipeline -> Settings -> Connections) before this stack can deploy --
CDK cannot perform that OAuth handshake. Everything else here is code.
"""

import aws_cdk as cdk
from aws_cdk import Stack, pipelines
from constructs import Construct

import config
from stage import LandingZoneStage


class PipelineStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        source = pipelines.CodePipelineSource.connection(
            f"{config.GITHUB_OWNER}/{config.GITHUB_REPO}",
            config.GITHUB_BRANCH,
            connection_arn=config.CODECONNECTIONS_ARN,
        )

        pipeline = pipelines.CodePipeline(
            self, "Pipeline",
            pipeline_name="lattice-landing-zone",
            self_mutation=True,
            synth=pipelines.ShellStep(
                "Synth",
                input=source,
                install_commands=[
                    "python3.12 -m venv .venv",
                    ". .venv/bin/activate",
                    "pip install -r requirements.txt",
                ],
                commands=[
                    ". .venv/bin/activate",
                    "npx cdk synth",
                ],
            ),
        )

        env = cdk.Environment(account=config.AWS_ACCOUNT_ID, region=config.AWS_REGION)
        landing_zone = LandingZoneStage(self, "LandingZone", env=env)

        # Manual approval gate before anything in the stage actually deploys
        # (SPEC.md Section 7: "I can remove it later").
        pipeline.add_stage(landing_zone, pre=[pipelines.ManualApprovalStep("PromoteToLandingZone")])
