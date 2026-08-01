"""pipeline_stack.py -- self-mutating CDK Pipeline sourced from CodeCommit (SPEC.md Section 7).

Deploy once by hand (`cdk deploy PipelineStack --profile deloitte`); every
push to config.CODECOMMIT_BRANCH after that re-synths, self-mutates the
pipeline if this file itself changed, and deploys LandingZoneStage
automatically -- gated behind the manual approval step below.

Originally sourced from GitHub via CodeConnections, but that requires a
GitHub App to be separately *installed* on the target repo (not just
OAuth-authorized) -- an easy-to-miss second step that left the Source stage
failing with "No Branch [main] found" even once the connection itself
showed AVAILABLE. Switched to CodeCommit (config.CODECOMMIT_REPO_NAME),
which authenticates with plain IAM credentials and has no such handshake.
The CodeCommit repo itself is created out of band (AWS CLI / console), not
by this stack -- see README.md.
"""

import aws_cdk as cdk
from aws_cdk import Stack, pipelines
from aws_cdk import aws_codecommit as codecommit
from aws_cdk import aws_iam as iam
from constructs import Construct

import config
from stage import LandingZoneStage


class PipelineStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        repo = codecommit.Repository.from_repository_name(
            self, "SourceRepo", config.CODECOMMIT_REPO_NAME
        )
        source = pipelines.CodePipelineSource.code_commit(repo, config.CODECOMMIT_BRANCH)

        pipeline = pipelines.CodePipeline(
            self, "Pipeline",
            pipeline_name="lattice-landing-zone",
            self_mutation=True,
            synth=pipelines.ShellStep(
                "Synth",
                input=source,
                install_commands=[
                    # The standard CodeBuild image manages Python versions via
                    # pyenv; 3.12.13 is installed but not selected globally by
                    # default, so `python3.12` isn't on PATH without this.
                    "pyenv global 3.12.13",
                    "python3.12 -m venv .venv",
                    ". .venv/bin/activate",
                    "pip install -r requirements.txt",
                ],
                commands=[
                    ". .venv/bin/activate",
                    # ThreeTierStack's web tier deploys app/frontend-next's
                    # static export (Next.js `output: 'export'`) -- built
                    # here, before synth, so Source.asset() finds real files
                    # at synth time. Node/npm ship on the standard CodeBuild
                    # image already (cdk itself needs it), no extra install.
                    "cd app/frontend-next && npm ci && npm run build && cd ../..",
                    "npx cdk synth",
                ],
                # TEMPORARY, deliberately -- Cloud WAN's core network +
                # attachments, Bedrock AgentCore/S3 Vectors/Knowledge Base,
                # and the SageMaker Async Inference endpoint all cost real
                # money on top of what this project already runs (see
                # README.md's cost warnings); this is meant to be deployed
                # for a demo, toured, then torn down, not left on as the
                # pipeline's permanent default. Revert these to unset (or
                # explicitly "false") once the demo is done, BEFORE the
                # next pipeline run, so none of these silently stay in the
                # synth target after `cdk destroy`.
                env={"ENABLE_CLOUDWAN": "true", "ENABLE_AI": "true", "ENABLE_SAGEMAKER": "true"},
            ),
        )

        env = cdk.Environment(account=config.AWS_ACCOUNT_ID, region=config.AWS_REGION)
        landing_zone = LandingZoneStage(self, "LandingZone", env=env)

        # Manual approval gate before anything in the stage actually deploys
        # (SPEC.md Section 7: "I can remove it later").
        pipeline.add_stage(landing_zone, pre=[pipelines.ManualApprovalStep("PromoteToLandingZone")])

        # cdk.context.json is deliberately NOT committed (see .gitignore --
        # it's an account-specific build artifact, out of place in a public
        # repo). Without it, every `cdk synth` -- including this one,
        # running fresh in CodeBuild with no local cache -- resolves each
        # ec2.Vpc's AZs via a live ec2:DescribeAvailabilityZones call
        # instead of a cached lookup. Grant it here rather than re-adding
        # the context file: a read-only, account-wide describe call is a
        # smaller, more honest footprint than a checked-in file that's
        # both stale-prone and tied to one specific AWS account.
        # build_pipeline() forces the underlying CodeBuild project (and its
        # role) to materialize now -- pipeline.synth_project is otherwise
        # only resolved lazily during CDK's own synth pass. Must run AFTER
        # add_stage() above, or the LandingZone stage/its ManualApprovalStep
        # would be locked out of the already-built pipeline.
        pipeline.build_pipeline()
        pipeline.synth_project.add_to_role_policy(iam.PolicyStatement(
            actions=["ec2:DescribeAvailabilityZones"], resources=["*"],
        ))
