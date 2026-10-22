# Reproducible Linux verification environments for U+ Reborn.
# Built once per lockfile/recipe fingerprint by tools/delivery.py; never mutates at runtime.

FROM python:3.13@sha256:debad600e8d6e754012528dc796488acc52438fea3d28596b87262df9b91c71e AS test

COPY --from=ghcr.io/astral-sh/uv:0.13.4@sha256:9f130421e930bfbdc563c00740466603f8723158e90297d07957c2294640262b /uv /usr/local/bin/uv

ARG NODE_VERSION=22.23.3
ARG NODE_SHA256=df450af89261115ef9f9e3830c3eeb2cc9213b63c720b1af623cb5dcbe2e02de
RUN curl -fsSL "https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-x64.tar.xz" -o /tmp/node.tar.xz \
    && echo "${NODE_SHA256}  /tmp/node.tar.xz" | sha256sum -c - \
    && tar -xJf /tmp/node.tar.xz -C /usr/local --strip-components=1 \
    && rm /tmp/node.tar.xz \
    && mkdir -p /root/.config/ruff \
    && printf '[lint]\nextend-ignore = ["EXE002"]\n' > /root/.config/ruff/ruff.toml

ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_LINK_MODE=copy \
    PATH="/opt/venv/bin:/usr/local/bin:${PATH}"

ARG SMOKE_COMPONENTS="homeassistant persistent_notification http api websocket_api config logger diagnostics dhcp sensor binary_sensor"
WORKDIR /tmp/build
COPY pyproject.toml uv.lock README.md ./
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

FROM ghcr.io/home-assistant/home-assistant:2026.10.2@sha256:33afb0659d786213264c2211f8d91c5fe87461c56eba6a3a546eb05fc71e89f4 AS core-current
COPY --from=test /usr/local/bin/node /usr/local/bin/node
ENTRYPOINT []
WORKDIR /workspace
