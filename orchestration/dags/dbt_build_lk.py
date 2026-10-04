"""ADR-001 item 8: a DAG run executes item 3's dbt build inside the Airflow container.

Item 8 measures the RAM budget with a dbt build running in Airflow while the clickstream load and
item 2's MERGE job run. Under LocalExecutor the task runs in the scheduler's cgroup, so the
scheduler's `mem_limit` holds the dbt process. The project mount (/opt/analytics/dbt) is read-only,
so the task copies the tracked project files to /tmp/dbt-work first: dbt writes target/ and logs/
there, and DuckDB's Iceberg writer makes data/. DuckDB's memory_limit comes from the lk
output in profiles.yml. This file holds no secret and no address beyond the catalog endpoint that profile
already names; Lakekeeper vends the storage credentials. The dbt CLI is /opt/dbt/bin/dbt, never
the PATH.
"""

from airflow.providers.standard.operators.bash import BashOperator
from airflow.sdk import DAG

DBT_BUILD = """set -euo pipefail
rm -rf /tmp/dbt-work
mkdir -p /tmp/dbt-work
cd /opt/analytics/dbt
cp -R dbt_project.yml profiles.yml macros models seeds /tmp/dbt-work/
cd /tmp/dbt-work
DO_NOT_TRACK=1 /opt/dbt/bin/dbt build --target lk --select +inc_v3 \\
  --project-dir /tmp/dbt-work --profiles-dir /tmp/dbt-work --vars '{batch: 2}'
"""

with DAG(dag_id="dbt_build_lk", schedule=None, catchup=False):
    BashOperator(task_id="dbt_build", bash_command=DBT_BUILD)
