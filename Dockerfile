FROM python:3.11-slim

WORKDIR /usr/src/app

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Copy dependency files first for layer caching
COPY pyproject.toml uv.lock ./

# Install production deps into the venv
RUN uv sync --frozen --no-dev

# Copy application source
COPY . .

# Add the venv to PATH so `python` resolves to the venv's interpreter directly,
# avoiding `uv run` which re-checks and recreates the venv on every container start.
ENV PATH="/usr/src/app/.venv/bin:$PATH"

CMD ["python", "main.py"]
