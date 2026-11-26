# syntax=docker/dockerfile:1
#
# Build context: repository root (needed for the shared VERSION file, ADR-009).
# Targets:
#   dev   — Vite dev server; src/ is bind-mounted by compose.yaml, /api is proxied to the backend.
#   build — production bundle in /app/dist.
#   dist  — only the static files: docker build -f apps/frontend/Dockerfile --target dist -o out .
#           (served by Nginx in the production profile, roadmap Phase 11).

ARG NODE_IMAGE=node:22-alpine

FROM ${NODE_IMAGE} AS deps
WORKDIR /app
COPY --chown=node:node apps/frontend/package.json apps/frontend/package-lock.json apps/frontend/.npmrc ./
# Use the npm version pinned in package.json "packageManager" (the image ships an older npm).
RUN --mount=type=cache,target=/root/.npm \
    npm install --global --no-audit --no-fund "$(node -p "require('./package.json').packageManager")" \
    && chown node:node /app
USER node
RUN --mount=type=cache,target=/home/node/.npm,uid=1000,gid=1000 \
    npm ci --no-audit --no-fund \
    && cp package-lock.json node_modules/.image-package-lock.json

FROM deps AS dev
COPY --chown=node:node apps/frontend/ ./
COPY VERSION /VERSION
EXPOSE 5173
# compose.yaml mounts the repository's package-lock.json: with dependencies newer than this image, the dev server
# would fail on the first import of a new package. Stop with the fix instead.
CMD ["sh", "-c", "if ! cmp -s package-lock.json node_modules/.image-package-lock.json; then echo 'apps/frontend/package-lock.json changed after this image was built: run docker compose up --build frontend' >&2; exit 1; fi; exec npm run dev -- --host 0.0.0.0"]

FROM deps AS build
COPY --chown=node:node apps/frontend/ ./
COPY VERSION /VERSION
RUN npm run build

FROM scratch AS dist
COPY --from=build /app/dist /
