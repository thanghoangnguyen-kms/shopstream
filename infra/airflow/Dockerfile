# Airflow 3.3.2 for the `orchestration` profile (ADR-001 item 6 and, in Phase 5, item 8). The DAGs and
# the plugins are bind-mounted at run time (../orchestration), so no code and no secret is baked in.
# The two Kafka providers are installed on top of the base image under Airflow's official constraints
# file for 3.3.2 on Python 3.13. The constraints file is fetched at build time, so the install is
# pinned by version but not hermetic, like the spike image's apt step.
FROM docker.io/apache/airflow:3.3.2-python3.13@sha256:e9982ad3f49a60418622e1baf80c34b0daf3fa5091a336cb5ee61f0cdd189371

RUN pip install --no-cache-dir "apache-airflow==3.3.2" "apache-airflow-providers-apache-kafka==2.0.0" "apache-airflow-providers-common-messaging==2.1.0" --constraint "https://raw.githubusercontent.com/apache/airflow/constraints-3.3.2/constraints-3.13.txt"
