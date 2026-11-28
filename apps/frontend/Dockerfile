# syntax=docker/dockerfile:1
#
# Build context: repository root (needed for the shared VERSION file, ADR-009).
# Targets:
#   dev   — Vite dev server; src/ is bind-mounted by compose.yaml, /api is proxied to the backend.
#   build — production bundle in /app/dist.
#   dist  — only the static files: docker build -f apps/frontend/Dockerfile --target dist -o out .
#   web   — Nginx of the production stack (compose.prod.yaml, ADR-023 §4): the bundle plus deploy/nginx/,
#           unprivileged (ports 8080/8443), read-only root filesystem with a tmpfs /tmp.

ARG NODE_IMAGE=node:22-alpine
ARG NGINX_IMAGE=nginx:1.30.7-alpine3.24

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

FROM ${NGINX_IMAGE} AS web
# Only ${PLUMMY_*} variables are substituted in the templates; nginx's own $variables stay as they are.
ENV NGINX_ENVSUBST_OUTPUT_DIR=/tmp/nginx-conf \
    NGINX_ENVSUBST_FILTER=PLUMMY_ \
    PLUMMY_SERVER_NAME=_ \
    PLUMMY_CLIENT_MAX_BODY_SIZE=60m \
    PLUMMY_STREAM_READ_TIMEOUT=660s
USER root
# Security updates of Alpine packages published after the tag was built (Trivy, SECURITY_MODEL §19).
RUN apk upgrade --no-cache && rm -f /etc/nginx/conf.d/default.conf
COPY deploy/nginx/nginx.conf /etc/nginx/nginx.conf
COPY deploy/nginx/security-headers.conf deploy/nginx/proxy-api.conf /etc/nginx/plummy/
COPY deploy/nginx/templates/ /etc/nginx/templates/
COPY --chmod=0755 deploy/nginx/05-plummy-tmp.sh /docker-entrypoint.d/
COPY --from=build /app/dist /usr/share/nginx/html
USER 101
EXPOSE 8080 8443
