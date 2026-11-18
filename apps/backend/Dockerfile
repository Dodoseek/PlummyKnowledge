# syntax=docker/dockerfile:1
#
# Build context: repository root (needed for the shared VERSION file, ADR-009).
#   docker build -f apps/backend/Dockerfile --target prod .
# Targets:
#   dev  — all dependencies (incl. test/lint tools), code is bind-mounted by compose.yaml, auto-reload.
#   prod — runtime dependencies only, non-root, no build tools.

ARG PYTHON_IMAGE=python:3.13-slim
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.13.8

FROM ${UV_IMAGE} AS uv

FROM ${PYTHON_IMAGE} AS base
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PYTHONPATH=/app/src \
    PATH=/opt/venv/bin:$PATH
RUN groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --no-create-home --shell /usr/sbin/nologin app \
    && install -d -m 0700 -o app -g app /var/lib/plummy/bootstrap
# /var/lib/plummy/bootstrap: one-time setup token (ADR-006). A named volume mounted here inherits the
# owner and mode 0700, so only the `app` user can read the token file.
WORKDIR /app

# ---- runtime dependencies (locked) ----
FROM base AS deps
COPY --from=uv /uv /usr/local/bin/uv
COPY apps/backend/pyproject.toml apps/backend/uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project

# ---- development ----
FROM base AS dev
COPY --from=uv /uv /usr/local/bin/uv
COPY apps/backend/pyproject.toml apps/backend/uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-install-project
COPY apps/backend/alembic.ini ./
COPY apps/backend/alembic ./alembic
COPY apps/backend/src ./src
COPY apps/backend/tests ./tests
COPY VERSION /VERSION
RUN chown app:app /app
USER app
EXPOSE 8000
# The reloader keeps running when the app fails to start, so validate the configuration first:
# an invalid configuration stops the container with exit code 1 (same as the prod target).
CMD ["sh", "-c", "python -m plummy.check_config && exec uvicorn --factory plummy.main:create_app --app-dir src --host 0.0.0.0 --port 8000 --no-access-log --reload --reload-dir src"]

# ---- production ----
FROM base AS prod
COPY --from=deps /opt/venv /opt/venv
COPY apps/backend/alembic.ini ./
COPY apps/backend/alembic ./alembic
COPY apps/backend/src ./src
COPY VERSION /VERSION
USER app
EXPOSE 8000
CMD ["uvicorn", "--factory", "plummy.main:create_app", "--app-dir", "src", \
     "--host", "0.0.0.0", "--port", "8000", "--no-access-log", "--no-server-header"]
