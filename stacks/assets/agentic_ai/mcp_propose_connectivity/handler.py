"""propose-connectivity MCP tool -- the one tool in this project that can
turn an agent's output into a real change, and it does so WITHOUT ever
calling a single AWS mutation API against network/IAM/compute resources.

It drafts a connectivity-request file, commits it to a new CodeCommit
branch, and opens a pull request against config.CODECOMMIT_BRANCH (main) --
the same branch pipeline_stack.py's self-mutating CDK Pipeline watches. A
human (or, if this project later wires up auto-merge-on-approval, the
pipeline itself) still has to merge it before anything deploys. This
Lambda's IAM role only grants CodeCommit branch/commit/PR actions -- no
ec2:*, no vpc-lattice:*, nothing that could touch live infrastructure
directly, by construction, not just by convention.
"""

import json
import os
import time

import boto3

codecommit = boto3.client("codecommit")

REPOSITORY_NAME = os.environ["REPOSITORY_NAME"]
BASE_BRANCH = os.environ.get("BASE_BRANCH", "main")
AUTHOR_NAME = "agentic-ai-connectivity-planner"
AUTHOR_EMAIL = "agentic-ai@example.invalid"


def handler(event, context):
    summary = event.get("summary", "")
    details = event.get("details", "")
    if not summary or not details:
        return {"error": "missing 'summary' or 'details'"}

    ts = int(time.time())
    branch_name = f"connectivity-proposal-{ts}"
    file_path = f"connectivity-requests/{ts}.json"

    base_branch_info = codecommit.get_branch(repositoryName=REPOSITORY_NAME, branchName=BASE_BRANCH)
    parent_commit_id = base_branch_info["branch"]["commitId"]

    codecommit.create_branch(repositoryName=REPOSITORY_NAME, branchName=branch_name, commitId=parent_commit_id)

    file_content = json.dumps({
        "summary": summary,
        "details": details,
        "proposed_by": AUTHOR_NAME,
        "proposed_at": ts,
        "status": "pending-review",
    }, indent=2)

    commit_resp = codecommit.create_commit(
        repositoryName=REPOSITORY_NAME,
        branchName=branch_name,
        parentCommitId=parent_commit_id,
        authorName=AUTHOR_NAME,
        email=AUTHOR_EMAIL,
        commitMessage=f"Propose connectivity change: {summary}",
        putFiles=[{"filePath": file_path, "fileContent": file_content.encode("utf-8")}],
    )

    pr_resp = codecommit.create_pull_request(
        title=f"[agentic-ai] {summary}",
        description=(
            f"{details}\n\n---\nOpened automatically by the connectivity-planner agent. "
            f"This PR only adds a request file under {file_path} -- it does NOT contain any "
            "infrastructure change yet. A human should review the request, author the actual "
            "CDK change, and merge; the pipeline picks it up from there."
        ),
        targets=[{
            "repositoryName": REPOSITORY_NAME,
            "sourceReference": branch_name,
            "destinationReference": BASE_BRANCH,
        }],
    )

    return {
        "pull_request_id": pr_resp["pullRequest"]["pullRequestId"],
        "branch_name": branch_name,
        "commit_id": commit_resp["commitId"],
        "request_file": file_path,
    }
