FROM python:3.11-slim

WORKDIR /app

# Install uv
RUN pip install --no-cache-dir uv==0.4.18

# Copy dependency files first for layer caching
COPY pyproject.toml uv.lock ./

# Install runtime deps only (no dev extras)
RUN uv sync --frozen --no-dev

# Copy source
COPY src/ src/

# NemoClaw blueprint entry point
ENTRYPOINT ["python", "-m", "loomstack.runner"]
