# App-tier backend image, run as an AWS Lambda container function
# (threetier_stack.py's AppFunction). app.py is stdlib-only -- no pip
# install needed. ECR Public's Docker Hub mirror avoids Docker Hub's
# anonymous-pull rate limits during CDK Pipelines' asset-publishing
# CodeBuild runs.
#
# The AWS Lambda Web Adapter (a Lambda extension, not a code change) lets
# app.py keep running as a plain http.server process instead of being
# rewritten into a handler(event, context) function. The adapter registers
# with the Lambda Runtime API and proxies each invoke as a real HTTP
# request to localhost:$AWS_LWA_PORT -- the same container image, the same
# app.py, whether it ends up on ECS or on Lambda.
FROM public.ecr.aws/docker/library/python:3.12-slim
COPY --from=public.ecr.aws/awsguru/aws-lambda-adapter:0.9.1 /lambda-adapter /opt/extensions/lambda-adapter
COPY app.py /app.py
ENV PORT=8080
ENV AWS_LWA_PORT=8080
EXPOSE 8080
CMD ["python3", "/app.py"]
