# Spike toolbox for the `probe` one-shot and, from Plan 02-04, the `spark-job` one-shot.
# Scripts are bind-mounted at run time (../scripts:/app:ro), so no code and no secret is baked in.
FROM docker.io/library/python:3.13.15-slim-trixie@sha256:7c61056e61ac89e852de05f3dc6fa51a6dd2181797bceed46aa725dd7cb2cd3b

COPY --from=ghcr.io/astral-sh/uv:0.12.18@sha256:3adc3706091ce7c2fe595e669628caedd6d951551b92b258b7e7dbe06d9440bc /uv /usr/local/bin/uv

# UV_PYTHON_DOWNLOADS=never makes uv use this image's own Python 3.13.15.
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_NO_CACHE=1 \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH=/opt/venv/bin:$PATH

WORKDIR /src
COPY pyproject.toml uv.lock ./
COPY packages/generator/pyproject.toml packages/generator/pyproject.toml

# The locked probe clients (boto3 and the readers). Plan 02-04 extends this file with the JRE,
# the spark group, the jars and the DuckDB extensions.
RUN uv sync --frozen --only-group spike
