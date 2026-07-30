# Minimal image for the placeholder app-tier backend (threetier_stack.py's
# ECS Fargate service). app.py is stdlib-only -- no pip install needed.
# ECR Public's Docker Hub mirror avoids Docker Hub's anonymous-pull rate
# limits during CDK Pipelines' asset-publishing CodeBuild runs.
FROM public.ecr.aws/docker/library/python:3.12-slim
COPY app.py /app.py
ENV PORT=8080
EXPOSE 8080
CMD ["python3", "/app.py"]
