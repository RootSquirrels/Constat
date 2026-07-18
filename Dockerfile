# syntax=docker/dockerfile:1
#
# Constat API image — serves BOTH the long-running API (default CMD) and
# the one-off daily scan task (ECS command override, see infra/ecs.tf).
#
# STATUS (2026-07-18): UNVALIDATED. Written without a Docker daemon on the
# dev machine — never built, never pushed. First build happens on an
# operator machine or in CI; expect to iterate on paths/uv flags then.
#
# Layout constraint: this is a uv WORKSPACE (root pyproject.toml +
# uv.lock, members under packages/* and apps/api). The build must run at
# the repo root so `uv sync` can resolve workspace members from source.

# ---- build stage: resolve the workspace into a self-contained venv ----
FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim AS build

WORKDIR /app

# Compile bytecode at install (faster cold start on Fargate) and copy
# instead of hardlinking so the venv survives the stage boundary.
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

# Workspace manifests + sources. No fine-grained per-member COPY layering:
# the workspace is small, and a stable single source layer beats cache
# cleverness that breaks every time a member is added.
COPY pyproject.toml uv.lock .python-version ./
COPY packages ./packages
COPY apps/api ./apps/api

# --frozen: respect the committed uv.lock (same as CI).
# --all-packages: install every workspace member (api + connectors + insights).
# --no-dev: no pytest/ruff/mypy in the image.
# --no-editable: copy workspace code into site-packages instead of linking
#   back to /app sources — the runtime stage then only needs the venv.
RUN uv sync --frozen --all-packages --no-dev --no-editable

# ---- runtime stage: slim python + the venv, non-root ----
FROM python:3.13-slim-bookworm

WORKDIR /app

RUN useradd --create-home --uid 10001 appuser

COPY --from=build /app/.venv /app/.venv

# Migrations are raw SQL applied out-of-band (no Alembic yet — see
# infra/README.md). They ship in the image so the one-off "migrate"
# Fargate task can run them against the private RDS instance.
COPY db/migrations ./db/migrations

ENV PATH="/app/.venv/bin:$PATH" \
    CONSTAT_ENV=pilot \
    CONSTAT_LOG_JSON=1
# Secrets (CONSTAT_DATABASE_URL, CONSTAT_API_KEY, scan targets) are
# injected by ECS from Secrets Manager — never baked in here.

USER appuser
EXPOSE 8000

# No Docker HEALTHCHECK: the slim image has no curl, and the ECS task
# definition already runs a python-based container health check
# (infra/ecs.tf). One health mechanism, in one place.
CMD ["uvicorn", "constat_api.main:app", "--host", "0.0.0.0", "--port", "8000"]
