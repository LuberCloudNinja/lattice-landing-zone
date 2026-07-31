"""agent-orchestrator Lambda -- fronts API Gateway, drives a Bedrock Converse
tool-use loop, and reads/writes the three-tier memory (agentic_ai_stack.py's
module docstring has the full tier writeup).

Tool dispatch is direct `lambda:InvokeFunction` against the MCP tool Lambdas
(TOOL_FUNCTION_ARNS below), NOT a live MCP JSON-RPC round-trip to the
AgentCore Gateway -- an MCP client library isn't available in the base
Lambda Python runtime, and this project's cdk-constraint is "verify what
exists, don't invent" rather than vendoring an untested dependency for a
lab. The SAME Lambdas are also registered as the Gateway's MCP targets
(agentic_ai_stack.py), so any external MCP client (Bedrock Agent Studio, a
different agent framework, a human debugging with an MCP inspector) gets
the identical tool surface through the real MCP protocol -- this Lambda is
just one particular caller taking a cheaper path to the same tools.
"""

import json
import os
import time

import boto3

bedrock = boto3.client("bedrock-runtime")
lambda_client = boto3.client("lambda")
dynamodb = boto3.resource("dynamodb")
s3 = boto3.client("s3")

MODEL_ID = os.environ["BEDROCK_MODEL_ID"]
WORKING_MEMORY_TABLE = os.environ["WORKING_MEMORY_TABLE"]
DURABLE_MEMORY_BUCKET = os.environ["DURABLE_MEMORY_BUCKET"]
TOOL_FUNCTION_ARNS = json.loads(os.environ["TOOL_FUNCTION_ARNS"])  # {tool_name: lambda_arn}
AGENT_ROLE_NAME = os.environ.get("AGENT_ROLE_NAME", "network-operator")
WORKING_MEMORY_TTL_SECONDS = int(os.environ.get("WORKING_MEMORY_TTL_SECONDS", "3600"))
MAX_TOOL_ITERATIONS = 6

SYSTEM_PROMPTS = {
    # Read-only: every tool it can reach only ever describes state, never
    # changes it -- the IAM role backing this persona (agentic_ai_stack.py's
    # NetworkOperatorAgentRole) has no invoke grant on propose-connectivity.
    "network-operator": (
        "You are the network-operator agent for a hybrid VPC Lattice landing zone lab. "
        "You can inspect Kafka resource-gateway status, search prior agent memory, check "
        "ALB/VPN network health, read governance/compliance findings, read Cloud WAN topology, "
        "and read anomaly-detection findings. You cannot change any infrastructure -- if asked "
        "to fix or create connectivity, say so and suggest the connectivity-planner agent instead."
    ),
    # The only persona with tool access to propose-connectivity, and even
    # that tool never mutates infrastructure directly -- it opens a
    # CodeCommit pull request for a human/pipeline to merge.
    "connectivity-planner": (
        "You are the connectivity-planner agent for a hybrid VPC Lattice landing zone lab. "
        "You have every network-operator tool PLUS propose-connectivity, which drafts a CDK "
        "change and opens a CodeCommit pull request -- it never touches AWS resources directly. "
        "Use it only when the user has described a concrete connectivity need; always summarize "
        "what the PR proposes before or after opening it."
    ),
}

TOOL_SPECS = [
    {
        "toolSpec": {
            "name": "query_kafka",
            "description": "Get the status of the on-prem Kafka broker's VPC Lattice resource gateway/configuration.",
            "inputSchema": {"json": {"type": "object", "properties": {}}},
        }
    },
    {
        "toolSpec": {
            "name": "search_memory",
            "description": "Semantic search over durable agent memory (Bedrock Knowledge Base backed by S3 Vectors).",
            "inputSchema": {
                "json": {
                    "type": "object",
                    "properties": {"query": {"type": "string", "description": "Natural-language search query"}},
                    "required": ["query"],
                }
            },
        }
    },
    {
        "toolSpec": {
            "name": "get_network_health",
            "description": "Get ALB target-group health and Site-to-Site VPN tunnel status.",
            "inputSchema": {"json": {"type": "object", "properties": {}}},
        }
    },
    {
        "toolSpec": {
            "name": "query_governance",
            "description": "Get a summary of active Security Hub findings and AWS Config compliance state.",
            "inputSchema": {"json": {"type": "object", "properties": {}}},
        }
    },
    {
        "toolSpec": {
            "name": "cloudwan_topology",
            "description": "Get the AWS Cloud WAN core network's segments and attachments (only if that layer is enabled).",
            "inputSchema": {"json": {"type": "object", "properties": {}}},
        }
    },
    {
        "toolSpec": {
            "name": "detect_anomalies",
            "description": "Get the latest VPC Flow Logs anomaly-detection findings (only if SageMaker layer is enabled).",
            "inputSchema": {"json": {"type": "object", "properties": {}}},
        }
    },
    {
        "toolSpec": {
            "name": "propose_connectivity",
            "description": (
                "Draft a connectivity change and open a CodeCommit pull request for it. "
                "Never mutates infrastructure directly -- connectivity-planner persona only."
            ),
            "inputSchema": {
                "json": {
                    "type": "object",
                    "properties": {
                        "summary": {"type": "string", "description": "One-line summary of the requested change"},
                        "details": {"type": "string", "description": "Full description of the connectivity need"},
                    },
                    "required": ["summary", "details"],
                }
            },
        }
    },
]


def _invoke_tool(tool_name: str, tool_input: dict) -> dict:
    fn_arn = TOOL_FUNCTION_ARNS.get(tool_name)
    if not fn_arn:
        return {"error": f"unknown tool: {tool_name}"}
    resp = lambda_client.invoke(
        FunctionName=fn_arn,
        InvocationType="RequestResponse",
        Payload=json.dumps(tool_input).encode("utf-8"),
    )
    payload = json.loads(resp["Payload"].read())
    if resp.get("FunctionError"):
        return {"error": str(payload)}
    return payload


def _load_working_memory(session_id: str) -> list:
    table = dynamodb.Table(WORKING_MEMORY_TABLE)
    item = table.get_item(Key={"session_id": session_id}).get("Item")
    return item["messages"] if item else []


def _save_working_memory(session_id: str, messages: list) -> None:
    table = dynamodb.Table(WORKING_MEMORY_TABLE)
    table.put_item(Item={
        "session_id": session_id,
        "messages": messages,
        "updated_at": int(time.time()),
        "ttl": int(time.time()) + WORKING_MEMORY_TTL_SECONDS,
    })


def _append_durable_transcript(session_id: str, turn: dict) -> None:
    key = f"transcripts/{session_id}.jsonl"
    try:
        existing = s3.get_object(Bucket=DURABLE_MEMORY_BUCKET, Key=key)["Body"].read().decode("utf-8")
    except s3.exceptions.NoSuchKey:
        existing = ""
    s3.put_object(Bucket=DURABLE_MEMORY_BUCKET, Key=key, Body=(existing + json.dumps(turn) + "\n").encode("utf-8"))


def handler(event, context):
    body = json.loads(event.get("body") or "{}")
    prompt = body.get("prompt", "")
    session_id = body.get("session_id") or context.aws_request_id
    if not prompt:
        return {"statusCode": 400, "body": json.dumps({"error": "missing 'prompt'"})}

    messages = _load_working_memory(session_id)
    messages.append({"role": "user", "content": [{"text": prompt}]})

    final_text = ""
    for _ in range(MAX_TOOL_ITERATIONS):
        resp = bedrock.converse(
            modelId=MODEL_ID,
            system=[{"text": SYSTEM_PROMPTS.get(AGENT_ROLE_NAME, SYSTEM_PROMPTS["network-operator"])}],
            messages=messages,
            toolConfig={"tools": TOOL_SPECS},
        )
        output_message = resp["output"]["message"]
        messages.append(output_message)

        if resp["stopReason"] != "tool_use":
            final_text = "".join(block.get("text", "") for block in output_message["content"])
            break

        tool_results = []
        for block in output_message["content"]:
            if "toolUse" not in block:
                continue
            tool_use = block["toolUse"]
            result = _invoke_tool(tool_use["name"], tool_use.get("input", {}))
            tool_results.append({
                "toolResult": {
                    "toolUseId": tool_use["toolUseId"],
                    "content": [{"json": result}],
                }
            })
        messages.append({"role": "user", "content": tool_results})
    else:
        final_text = "Reached the tool-call limit for this turn without a final answer."

    _save_working_memory(session_id, messages)
    _append_durable_transcript(session_id, {"session_id": session_id, "prompt": prompt, "response": final_text, "ts": int(time.time())})

    return {
        "statusCode": 200,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({"session_id": session_id, "response": final_text}),
    }
