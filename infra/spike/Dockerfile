# Spike toolbox for the `probe` one-shot and the `spark-job` one-shot (item 14's image).
# Scripts are bind-mounted at run time (../scripts:/app:ro), so no code and no secret is baked in.
# Everything the jobs need is baked and checksum-verified here, so nothing resolves from the
# network at run time and the evidence reruns on the same pins.
FROM docker.io/library/python:3.13.15-slim-trixie@sha256:7c61056e61ac89e852de05f3dc6fa51a6dd2181797bceed46aa725dd7cb2cd3b

COPY --from=ghcr.io/astral-sh/uv:0.12.18@sha256:3adc3706091ce7c2fe595e669628caedd6d951551b92b258b7e7dbe06d9440bc /uv /usr/local/bin/uv

# Debian's JRE 21 for Spark. apt is not hermetic (the package version follows the Debian archive),
# so spark_v3_job.py records the JVM's runtime version in its output and a drift shows on rerun.
RUN apt-get update \
    && apt-get install -y --no-install-recommends openjdk-21-jre-headless \
    && rm -rf /var/lib/apt/lists/*

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

# The locked probe clients and readers (spike group) and PySpark 4.1.3 (spark group).
RUN uv sync --frozen --only-group spike --only-group spark

# Iceberg runtime for Spark 4.1 / Scala 2.13, into PySpark's jar directory. ADD --checksum fails
# the build on a mismatch; the chmod flag keeps each file readable by uid 65534 (a remote ADD would
# otherwise make it root-owned mode 600).
ADD --checksum=sha256:d6ea6c5d099288daeb7d5a92061bd3d7d8f296492632b42378e5f2f0e3066242 --chmod=644 https://repo1.maven.org/maven2/org/apache/iceberg/iceberg-spark-runtime-4.1_2.13/1.11.0/iceberg-spark-runtime-4.1_2.13-1.11.0.jar /opt/venv/lib/python3.13/site-packages/pyspark/jars/

# The AWS SDK bundle Iceberg's S3FileIO needs.
ADD --checksum=sha256:38f01da7e96850cdd05e6616d758b77b43314b712a8808e3f9a824d56976162f --chmod=644 https://repo1.maven.org/maven2/org/apache/iceberg/iceberg-aws-bundle/1.11.0/iceberg-aws-bundle-1.11.0.jar /opt/venv/lib/python3.13/site-packages/pyspark/jars/

# DuckDB's iceberg, httpfs and avro extensions, baked outside any home directory so a reader
# running as another uid never downloads one (DuckDB checks extension signatures on load).
RUN python -c "import duckdb; c = duckdb.connect(config={'extension_directory': '/opt/duckdb/extensions'}); [(c.execute('INSTALL ' + e), c.execute('LOAD ' + e)) for e in ('iceberg', 'httpfs', 'avro')]" \
    && chmod -R a+rX /opt/duckdb
