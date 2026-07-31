"""Placeholder entry point for the Bedrock AgentCore Runtime resource.

AgentCore Runtime's real invocation contract is an always-on HTTP server
(distinct from a Lambda request/response handler) -- wiring the full
agent-orchestrator loop (stacks/assets/agentic_ai/orchestrator/handler.py)
to that contract is future work, out of scope for this lab. This file
exists so agentic_ai_stack.py's CfnRuntime resources have a real,
deployable S3 code artifact to point at and demonstrate the construct
end-to-end -- the actual working demo path is API Gateway -> the
agent-orchestrator Lambda, not this Runtime resource.
"""

print("AgentCore Runtime stub -- see this file's module docstring.")
