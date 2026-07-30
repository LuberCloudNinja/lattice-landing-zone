# Minimal image for the PrivateLink demo provider app (privatelink_stack.py's
# ECS Fargate service). app.py is stdlib-only -- no pip install needed.
FROM public.ecr.aws/docker/library/python:3.12-slim
COPY app.py /app.py
ENV PORT=8080
EXPOSE 8080
CMD ["python3", "/app.py"]
