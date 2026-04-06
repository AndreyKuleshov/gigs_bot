FROM python:3.11-slim

WORKDIR /usr/src/app

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Copy dependency files first for layer caching
COPY pyproject.toml uv.lock ./

# Install production deps only (no dev extras)
RUN uv sync --frozen --no-dev

# Copy application source
COPY . .

# Use uv's managed Python to run the app
CMD ["uv", "run", "python", "main.py"]
