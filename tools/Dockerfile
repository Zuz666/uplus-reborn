# Reproducible Linux verification environments for U+ Reborn.
# Built from explicit stage inputs by tools/delivery.py; never mutates at runtime.

FROM python:3.13@sha256:debad600e8d6e754012528dc796488acc52438fea3d28596b87262df9b91c71e AS node

ARG NODE_VERSION=22.23.3
ARG NODE_SHA256=df450af89261115ef9f9e3830c3eeb2cc9213b63c720b1af623cb5dcbe2e02de
RUN curl -fsSL "https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-x64.tar.xz" -o /tmp/node.tar.xz \
    && echo "${NODE_SHA256}  /tmp/node.tar.xz" | sha256sum -c - \
    && tar -xJf /tmp/node.tar.xz -C /usr/local --strip-components=1 \
    && rm /tmp/node.tar.xz

FROM node AS test
COPY --from=ghcr.io/astral-sh/uv:0.13.4@sha256:9f130421e930bfbdc563c00740466603f8723158e90297d07957c2294640262b /uv /usr/local/bin/uv

ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_LINK_MODE=copy \
    PATH="/opt/venv/bin:/usr/local/bin:${PATH}"

ARG SMOKE_COMPONENTS="homeassistant persistent_notification http api websocket_api config logger diagnostics dhcp sensor binary_sensor"
WORKDIR /tmp/build
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --group test --no-group research \
    && SMOKE_COMPONENTS="${SMOKE_COMPONENTS}" /opt/venv/bin/python -c "\
import importlib.util, importlib.metadata, json, os; \
from pathlib import Path; from packaging.requirements import Requirement; \
comp = Path(next(iter(importlib.util.find_spec('homeassistant').submodule_search_locations))) / 'components'; \
pending, seen, reqs = os.environ['SMOKE_COMPONENTS'].split(), set(), set(); \
[seen.add(d) or reqs.update(m.get('requirements', ())) or pending.extend(m.get('dependencies', ())) for d in pending if d not in seen for m in (json.loads((comp / d / 'manifest.json').read_text(encoding='utf-8')),)]; \
missing = [r for r in sorted(reqs) if importlib.metadata.version(Requirement(r).name) not in Requirement(r).specifier]; \
assert not missing, f'Locked smoke prerequisites do not match Core: {missing}'" \
    && rm -rf /tmp/build /root/.cache
WORKDIR /workspace

FROM ghcr.io/home-assistant/home-assistant:2026.10.4@sha256:23f4dfbb69ce76315dbc4de7915504f1c2dcecd7ca2648113a493a385972e133 AS core-current
COPY --from=node:22.23.3-alpine3.24@sha256:2c752226d477b4a886378baa95b9af252be59301b725fdb0b7e15208131505a8 /usr/local/bin/node /usr/local/bin/node
COPY --from=node:22.23.3-alpine3.24@sha256:2c752226d477b4a886378baa95b9af252be59301b725fdb0b7e15208131505a8 /usr/local/LICENSE /usr/local/share/licenses/node/LICENSE
RUN test "$(node --version)" = "v22.23.3"
ENTRYPOINT []
WORKDIR /workspace
