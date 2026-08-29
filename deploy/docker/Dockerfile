# syntax=docker/dockerfile:1.7
FROM node:26-bookworm-slim AS web-build
WORKDIR /src
COPY package.json package-lock.json ./
COPY apps/web/package.json apps/web/package.json
COPY packages/ui/package.json packages/ui/package.json
COPY packages/shared-types/package.json packages/shared-types/package.json
COPY patches patches
RUN npm ci --ignore-scripts \
    && ./node_modules/.bin/patch-package --error-on-fail
COPY apps/web apps/web
COPY packages/ui packages/ui
COPY packages/shared-types packages/shared-types
RUN npm run build --workspace apps/web

FROM python:3.12-slim-bookworm AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HERMES_CONTROL_ENVIRONMENT=production \
    HERMES_CONTROL_CREATE_SCHEMA_ON_START=false \
    HERMES_CONTROL_STATIC_DIR=/opt/hermes-control/apps/api/static
RUN groupadd --system --gid 10001 hermes-control \
    && useradd --system --uid 10001 --gid hermes-control --home-dir /nonexistent --shell /usr/sbin/nologin hermes-control
WORKDIR /opt/hermes-control
COPY apps/api/pyproject.toml apps/api/pyproject.toml
COPY packages/hermes-client/pyproject.toml packages/hermes-client/pyproject.toml
COPY packages/hermes-client/hermes_client packages/hermes-client/hermes_client
COPY apps/api apps/api
COPY deploy/docker/entrypoint.sh deploy/docker/entrypoint.sh
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir ./packages/hermes-client ./apps/api
COPY --from=web-build /src/apps/web/dist apps/api/static
RUN mkdir -p /var/lib/hermes-control \
    && chmod 0755 deploy/docker/entrypoint.sh \
    && chown -R hermes-control:hermes-control /var/lib/hermes-control /opt/hermes-control
USER 10001:10001
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s --start-period=20s --retries=3 \
  CMD ["/opt/venv/bin/python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/v1/ready', timeout=2)"]
ENTRYPOINT ["/opt/hermes-control/deploy/docker/entrypoint.sh"]
CMD ["hermes_control_api.main:app", "--app-dir", "apps/api", "--host", "127.0.0.1", "--port", "8000", "--workers", "1", "--no-proxy-headers", "--ws-max-size", "4096"]
