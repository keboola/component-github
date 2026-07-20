FROM python:3.14-slim AS base
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /code/
COPY pyproject.toml uv.lock ./

ENV UV_PROJECT_ENVIRONMENT="/usr/local/"
RUN uv sync --no-dev --frozen

COPY src/ src/
COPY scripts/ scripts/

FROM base AS test
RUN uv sync --all-groups --frozen
COPY tests/ tests/
# component_config/ is needed by the schema-validation test (test_auth_and_sync); it is a
# build-time deliverable (uploaded to the Dev Portal), so it is copied into the test stage only.
COPY component_config/ component_config/
RUN uv run ruff check src/ tests/
CMD ["uv", "run", "pytest", "tests/", "-v"]

FROM base AS production
CMD ["python", "-u", "/code/src/component.py"]
