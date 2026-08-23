# Stage 1 — dashboard (Node 24 LTS, SPEC §2)
FROM node:24-slim AS dashboard
WORKDIR /build
COPY dashboard/package.json dashboard/package-lock.json ./
RUN npm ci
COPY dashboard/ ./
RUN npm run build            # → /build/dist

# Stage 2 — manager (Python 3.14, uv, hash-verified lock; SPEC §2)
FROM python:3.14-slim
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
 && rm -rf /var/lib/apt/lists/*          # git: manager-side clone (SPEC §4.3)
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
WORKDIR /app

# manager/pyproject.toml declares [tool.hatch.build.targets.wheel]
# packages = ["werft"] — hatchling needs the "werft" package source present
# to build/install the project itself. Sync deps only first (frozen, no
# project) so the dependency layer stays cached across source changes, then
# copy the source and re-sync to install the project on top of it.
COPY manager/pyproject.toml manager/uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY manager/werft ./werft
COPY manager/alembic.ini ./alembic.ini
RUN uv sync --frozen --no-dev
COPY --from=dashboard /build/dist /app/dashboard-dist
ENV WERFT_DASHBOARD_DIST=/app/dashboard-dist
EXPOSE 8420
# werft/app.py exposes create_app() (a factory), not a module-level `app` —
# uvicorn needs --factory to call it rather than import an ASGI instance.
CMD ["uv", "run", "--no-sync", "uvicorn", "--factory", "werft.app:create_app", "--host", "0.0.0.0", "--port", "8420"]
