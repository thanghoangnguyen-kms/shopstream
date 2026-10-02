# dbt-core 1.12.5 with dbt-duckdb 1.11.0 on DuckDB 1.5.5 for the `dbt-job` one-shot (items 3, 9, 4
# and 7), in two venvs: /opt/dbt builds the project and /opt/mf holds MetricFlow (`mf`, ADR-001
# item 4) from its own uv project. Both carry dbt-core 1.12.5. The project, the models and the
# scripts are bind-mounted at run time (../analytics, ../scripts), so no code and no secret is
# baked in. Each CLI is called by its absolute path (/opt/dbt/bin/dbt, /opt/mf/bin/dbt,
# /opt/mf/bin/mf), never from PATH, because two `dbt` binaries on one PATH would shadow each other.
FROM docker.io/library/python:3.13.15-slim-trixie@sha256:7c61056e61ac89e852de05f3dc6fa51a6dd2181797bceed46aa725dd7cb2cd3b

COPY --from=ghcr.io/astral-sh/uv:0.12.18@sha256:3adc3706091ce7c2fe595e669628caedd6d951551b92b258b7e7dbe06d9440bc /uv /usr/local/bin/uv

# UV_PYTHON_DOWNLOADS=never makes uv use this image's own Python 3.13.15. No UV_PROJECT_ENVIRONMENT
# and no PATH entry: the venv is chosen per command.
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_NO_CACHE=1 \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /src
COPY analytics/dbt/pyproject.toml analytics/dbt/uv.lock dbt/
COPY analytics/metricflow/pyproject.toml analytics/metricflow/uv.lock mf/

# The dbt-core-experimental-parser sdist's build backend downloads its platform wheel from the
# github.com/dbt-labs/dbt releases and fails on a sha256 that differs from the one inside the sdist.
RUN UV_PROJECT_ENVIRONMENT=/opt/dbt uv sync --frozen --project /src/dbt
RUN UV_PROJECT_ENVIRONMENT=/opt/mf uv sync --frozen --project /src/mf
