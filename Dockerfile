# Pin to specific image for reproducible builds
# To get the hash: docker pull public.ecr.aws/docker/library/python:3.12-slim
# Then use: python:3.12-slim@sha256:<hash>
FROM public.ecr.aws/docker/library/python:3.12-slim

WORKDIR /app

# Install curl for healthcheck, clean package cache to reduce image size
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Install dependencies from requirements.txt (cached layer)
COPY requirements.txt requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Create non-root user
RUN useradd -m -u 1000 bedrock_agentcore
USER bedrock_agentcore

EXPOSE 8080
EXPOSE 8000

# Copy application code
COPY agent/ agent/
COPY billing_agent.py billing_agent.py

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
  CMD ["curl", "-f", "http://localhost:8080/ping"]

CMD ["opentelemetry-instrument", "python", "-m", "billing_agent"]
