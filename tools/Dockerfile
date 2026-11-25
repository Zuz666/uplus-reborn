# Reproducible Linux verification environments for U+ Reborn.
# Built from explicit stage inputs by tools/delivery.py; never mutates at runtime.

FROM node:22.23.3-alpine3.24@sha256:2c752226d477b4a886378baa95b9af252be59301b725fdb0b7e15208131505a8 AS node

# Minimum supported Core matches hacs.json; only test harness + Ruff come from uv.lock.
FROM ghcr.io/home-assistant/home-assistant:2026.2.3@sha256:96fa92d83fa8dae987fbbbcf58b1fea1140985ff6a8517b37f7b65c76ef20133 AS test
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/LICENSE /usr/local/share/licenses/node/LICENSE
COPY --from=ghcr.io/astral-sh/uv:0.13.4@sha256:9f130421e930bfbdc563c00740466603f8723158e90297d07957c2294640262b /uv /usr/local/bin/uv

RUN test "$(node --version)" = "v22.23.3"

ARG SMOKE_COMPONENTS="homeassistant persistent_notification http api websocket_api config logger diagnostics dhcp sensor binary_sensor"
WORKDIR /tmp/build
COPY pyproject.toml uv.lock ./
RUN UV_EXTRA_INDEX_URL="" uv export --frozen --only-group test --no-emit-project --output-file /tmp/build/test-requirements.txt \
    && UV_EXTRA_INDEX_URL="" uv pip install --system --no-deps --require-hashes -r /tmp/build/test-requirements.txt \
    && SMOKE_COMPONENTS="${SMOKE_COMPONENTS}" python3 -c "\
import importlib.util, importlib.metadata, json, os; \
from pathlib import Path; from packaging.requirements import Requirement; \
comp = Path(next(iter(importlib.util.find_spec('homeassistant').submodule_search_locations))) / 'components'; \
pending, seen, reqs = os.environ['SMOKE_COMPONENTS'].split(), set(), set(); \
[seen.add(d) or reqs.update(m.get('requirements', ())) or pending.extend(m.get('dependencies', ())) for d in pending if d not in seen for m in (json.loads((comp / d / 'manifest.json').read_text(encoding='utf-8')),)]; \
missing = [r for r in sorted(reqs) if importlib.metadata.version(Requirement(r).name) not in Requirement(r).specifier]; \
assert not missing, f'Locked smoke prerequisites do not match Core: {missing}'" \
    && rm -rf /tmp/build /root/.cache
ENTRYPOINT []
WORKDIR /workspace

FROM ghcr.io/home-assistant/home-assistant:2026.11.2@sha256:493a07e8f6530fc640223f4a0a5248f20a61ca2f718bb1d31efbf08a2c5ea28f AS core-current
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/LICENSE /usr/local/share/licenses/node/LICENSE
RUN test "$(node --version)" = "v22.23.3"
ENTRYPOINT []
WORKDIR /workspace
