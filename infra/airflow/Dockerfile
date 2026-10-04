# Airflow 3.3.2 for the `orchestration` profile (ADR-001 item 6 and, in Phase 5, item 8). The DAGs and
# the plugins are bind-mounted at run time (../orchestration), so no code and no secret is baked in.
# The two Kafka providers are installed on top of the base image under Airflow's official constraints
# file for 3.3.2 on Python 3.13. The constraints file is fetched at build time, so the install is
# pinned by version but not hermetic, like the spike image's apt step.
FROM docker.io/apache/airflow:3.3.2-python3.13@sha256:e9982ad3f49a60418622e1baf80c34b0daf3fa5091a336cb5ee61f0cdd189371

RUN pip install --no-cache-dir "apache-airflow==3.3.2" "apache-airflow-providers-apache-kafka==2.0.0" "apache-airflow-providers-common-messaging==2.1.0" --constraint "https://raw.githubusercontent.com/apache/airflow/constraints-3.3.2/constraints-3.13.txt"

# Phase 5, item 8: the dbt_build_lk DAG runs item 3's dbt build in this image's scheduler (LocalExecutor),
# so the image carries item 3's dbt runtime in /opt/dbt (dbt-core 1.12.5, dbt-duckdb 1.11.0, DuckDB 1.5.5),
# synced from analytics/dbt's lock by the digest-pinned uv. The sync runs as root and the image goes back
# to the airflow user. There is no ENV line and nothing on PATH, so Airflow's runtime environment is
# unchanged: the dbt CLI is called as /opt/dbt/bin/dbt, never from PATH. UV_PYTHON_DOWNLOADS=never makes
# uv use this image's own Python 3.13. The dbt-core-experimental-parser sdist fetches its platform wheel
# from github.com at build time, so this sync is pinned by the lock's hashes but not hermetic.
USER root
COPY --from=ghcr.io/astral-sh/uv:0.12.18@sha256:3adc3706091ce7c2fe595e669628caedd6d951551b92b258b7e7dbe06d9440bc /uv /usr/local/bin/uv
COPY analytics/dbt/pyproject.toml analytics/dbt/uv.lock /src/dbt/
RUN UV_PROJECT_ENVIRONMENT=/opt/dbt UV_PYTHON_DOWNLOADS=never UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_NO_CACHE=1 uv sync --frozen --project /src/dbt
USER airflow
