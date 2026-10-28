FROM ghcr.io/astral-sh/uv:0.9.5 AS uv
FROM python:3.12-slim
COPY --from=uv /uv /usr/local/bin/uv
RUN apt-get update && apt-get install -y --no-install-recommends bash ca-certificates curl git && rm -rf /var/lib/apt/lists/*
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1 PATH="/app/.venv/bin:$PATH"
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY core core
COPY bootstrap bootstrap
COPY biz biz
COPY extensions extensions
COPY scripts/oath-cli scripts/oath-cli
COPY scripts/install-oath-cli scripts/install-oath-cli
RUN useradd --create-home --uid 10001 oath && mkdir -p /data /workspace && chown oath:oath /data /workspace && chmod +x scripts/oath-cli
USER oath
ENTRYPOINT ["python", "-m", "bootstrap"]
CMD ["gateway", "--config", "/app/oath.toml", "--profile", "docker"]
