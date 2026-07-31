"""search-memory MCP tool -- semantic search over the durable/semantic memory
tier: a Bedrock Knowledge Base backed by an S3 Vectors index
(agentic_ai_stack.py). Read-only (bedrock-agent-runtime:Retrieve).
"""

import os

import boto3

bedrock_agent_runtime = boto3.client("bedrock-agent-runtime")

KNOWLEDGE_BASE_ID = os.environ["KNOWLEDGE_BASE_ID"]
DATA_SOURCE_ID = os.environ.get("DATA_SOURCE_ID", "")


def handler(event, context):
    query = event.get("query", "")
    if not query:
        return {"error": "missing 'query'"}

    resp = bedrock_agent_runtime.retrieve(
        knowledgeBaseId=KNOWLEDGE_BASE_ID,
        retrievalQuery={"text": query},
        retrievalConfiguration={"vectorSearchConfiguration": {"numberOfResults": 5}},
    )
    results = [
        {
            "content": r.get("content", {}).get("text", ""),
            "score": r.get("score"),
            "location": r.get("location"),
        }
        for r in resp.get("retrievalResults", [])
    ]
    return {"query": query, "results": results}
