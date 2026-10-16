FROM python:3.12-slim

# DejaVu covers Cyrillic and Uzbek Latin (oʻ, gʻ) for the ticket image.
RUN apt-get update \
    && apt-get install -y --no-install-recommends fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.12.5 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY . .
ENV PATH="/app/.venv/bin:$PATH"
RUN useradd --system --no-create-home bot && chown -R bot /app
USER bot

# Apply migrations, then start polling + background worker.
CMD ["sh", "-c", "alembic upgrade head && python -m app"]
