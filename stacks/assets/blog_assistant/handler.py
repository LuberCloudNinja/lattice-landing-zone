"""blog-assistant-ask handler -- POST /assistant/ask from the blog's
ChatConsole client component (app/frontend-next/components/ChatConsole.tsx).

Reached through CloudFront at /assistant/* on the same distribution as the
rest of the site (see blog_assistant_stack.py). Three AWS pieces work
together on every request:

  1. Retrieval Augmented Generation: bedrock-agent-runtime Retrieve against
     a Bedrock Knowledge Base backed by an S3 Vectors index
     (blog_assistant_stack.py), the same construct pattern already proven
     in agentic_ai_stack.py's semantic memory tier. The knowledge base is
     built from this project's own blog content, both the plain language
     and technical sections, so answers are grounded in what this project
     actually is rather than a general purpose model guessing.
  2. Generation: bedrock-runtime Converse against an Anthropic Claude model
     (config.BEDROCK_MODEL_ID), given the retrieved passages plus the
     conversation history as context.
  3. Memory: a single JSON object per conversationId in an S3 bucket,
     read at the start of the request and written back at the end. This is
     explicit, inspectable, first party memory, not a hidden session cache
     on the Bedrock side.
"""

import json
import os
import time

import boto3

bedrock_agent_runtime = boto3.client("bedrock-agent-runtime")
bedrock_runtime = boto3.client("bedrock-runtime")
s3 = boto3.client("s3")

KNOWLEDGE_BASE_ID = os.environ["KNOWLEDGE_BASE_ID"]
MODEL_ID = os.environ["MODEL_ID"]
MEMORY_BUCKET = os.environ["MEMORY_BUCKET"]
MAX_HISTORY_TURNS = int(os.environ.get("MAX_HISTORY_TURNS", "8"))

SYSTEM_PROMPT = (
    "You are the assistant embedded in Luber J Guilarte Hay's portfolio blog about the "
    "lattice-landing-zone project, a hybrid AWS landing zone built in CDK. You are a genuine "
    "expert in cloud architecture, AWS networking, security, containers, and the AI and machine "
    "learning services used in this project. Visitors will ask two kinds of questions: some are "
    "about this specific project (its stacks, its design decisions, why something was built a "
    "certain way), and some are broader questions about the underlying technologies it uses, "
    "such as VPC Lattice, Transit Gateway, Cloud WAN, PrivateLink, Gateway Load Balancer, "
    "Bedrock, SageMaker, Kafka, ECS Fargate, or CDK in general. Reference passages retrieved from "
    "this project's own documentation are provided below when available. Use them as the "
    "authoritative source for anything about this specific project. For general questions about "
    "the technologies themselves, beyond what the passages cover, answer from your own knowledge "
    "of AWS and cloud architecture the same way you would in any technical conversation. Only say "
    "you do not know when a question is genuinely unrelated to cloud, software, or this project, "
    "and in that case suggest the visitor read the full blog post or the source code on GitHub. "
    "Explain things in plain, direct sentences aimed at a technical reader. Do not use em dashes "
    "or en dashes as punctuation, use periods and commas instead. Keep answers focused and "
    "conversational, a few sentences to a couple of short paragraphs, going deeper when the "
    "question is genuinely technical."
)


def _cors_headers():
    return {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type",
        "Content-Type": "application/json",
    }


def _memory_key(conversation_id: str) -> str:
    return f"conversations/{conversation_id}.json"


def _load_history(conversation_id: str):
    try:
        obj = s3.get_object(Bucket=MEMORY_BUCKET, Key=_memory_key(conversation_id))
        return json.loads(obj["Body"].read())
    except s3.exceptions.NoSuchKey:
        return []
    except Exception:
        return []


def _save_history(conversation_id: str, history: list) -> None:
    trimmed = history[-(MAX_HISTORY_TURNS * 2):]
    s3.put_object(
        Bucket=MEMORY_BUCKET,
        Key=_memory_key(conversation_id),
        Body=json.dumps(trimmed).encode("utf-8"),
        ContentType="application/json",
        ServerSideEncryption="aws:kms",
    )


def _retrieve(question: str) -> str:
    # Retrieval augments the answer, it does not gate it -- if the knowledge
    # base call fails for any reason the assistant should still fall back to
    # answering from general AWS/cloud knowledge rather than 500ing.
    try:
        resp = bedrock_agent_runtime.retrieve(
            knowledgeBaseId=KNOWLEDGE_BASE_ID,
            retrievalQuery={"text": question},
            retrievalConfiguration={"vectorSearchConfiguration": {"numberOfResults": 6}},
        )
        passages = [r.get("content", {}).get("text", "") for r in resp.get("retrievalResults", [])]
        return "\n\n---\n\n".join(p for p in passages if p)
    except Exception:
        return ""


def handler(event, context):
    method = event.get("requestContext", {}).get("http", {}).get("method", "POST")
    if method == "OPTIONS":
        return {"statusCode": 204, "headers": _cors_headers()}

    try:
        body = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return {"statusCode": 400, "headers": _cors_headers(), "body": json.dumps({"error": "invalid json"})}

    question = str(body.get("question", "")).strip()
    conversation_id = str(body.get("conversationId") or "anonymous")[:128]
    if not question:
        return {"statusCode": 400, "headers": _cors_headers(), "body": json.dumps({"error": "missing question"})}

    context_text = _retrieve(question)
    history = _load_history(conversation_id)

    messages = []
    for turn in history:
        messages.append({"role": turn["role"], "content": [{"text": turn["text"]}]})

    grounded_question = (
        f"Reference passages from this project's own documentation (use these for anything "
        f"specific to this project; answer from your own knowledge for general technology "
        f"questions the passages do not cover):\n\n{context_text}\n\n"
        f"Visitor question: {question}"
        if context_text
        else question
    )
    messages.append({"role": "user", "content": [{"text": grounded_question}]})

    response = bedrock_runtime.converse(
        modelId=MODEL_ID,
        system=[{"text": SYSTEM_PROMPT}],
        messages=messages,
        inferenceConfig={"maxTokens": 1100, "temperature": 0.4},
    )
    answer = response["output"]["message"]["content"][0]["text"]

    history.append({"role": "user", "text": question, "ts": int(time.time())})
    history.append({"role": "assistant", "text": answer, "ts": int(time.time())})
    _save_history(conversation_id, history)

    return {
        "statusCode": 200,
        "headers": _cors_headers(),
        "body": json.dumps({"answer": answer}),
    }
