# Kafka Connect for the `streaming` profile: Debezium 3.6.3.Final on Connect 4.3.0, plus the Iceberg
# Kafka Connect sink 1.11.0 and the Confluent Avro converter 8.3.2, which the Debezium image lacks.
# Three stages, every base pinned by digest. Nothing is copied from the build context and no secret
# is baked in: the connector password reaches the container only as an environment variable.
#
# 1. sink-build: the sink is not published as a runtime artifact, so it is built from the 1.11.0
#    release commit (a 40-hex git ref, not a tag). The git tree has neither version.txt nor .git, so
#    the two files the release tarball ships are written by hand. Gradle resolves the build's
#    dependencies from Maven Central and the plugin portal at build time, so this stage is not
#    hermetic (like the spike image's apt step): the zip's sha256 is recorded in the image
#    (/kafka/iceberg-sink-zip.sha256) and the evidence rather than pinned.
FROM docker.io/library/gradle:8.14.4-jdk21-noble@sha256:cc90b5198f747c454f4a3e17495b626fbdfa7ac77f1ad08502c355e6f81e6772 AS sink-build
WORKDIR /src
ADD https://github.com/apache/iceberg.git#6976e020b894f6a6777704df2b8c4458cb291ae9 /src
RUN echo 1.11.0 > version.txt \
    && printf 'git.build.version=1.11.0\ngit.commit.id=6976e020b894f6a6777704df2b8c4458cb291ae9\ngit.commit.id.abbrev=6976e02\n' > iceberg-build.properties \
    && gradle --no-daemon -DsparkVersions= -DflinkVersions= -x test -x integrationTest \
        :iceberg-kafka-connect:iceberg-kafka-connect-runtime:distZip

# 2. unpack: the converter zip is fetched with ADD --checksum, which fails the build on a mismatch (the
#    chmod flag keeps it readable, a remote ADD would otherwise make it root-owned mode 600). Both zips are
#    extracted here so the final image needs no unzip tool.
FROM docker.io/library/python:3.13.15-slim-trixie@sha256:7c61056e61ac89e852de05f3dc6fa51a6dd2181797bceed46aa725dd7cb2cd3b AS unpack
COPY --from=sink-build /src/kafka-connect/kafka-connect-runtime/build/distributions/iceberg-kafka-connect-runtime-1.11.0.zip /in/sink.zip
ADD --checksum=sha256:f849f49ae500d1c02f94a50d1dd4b3e63d26433843007ac114a7f8e9fb3d6be8 --chmod=644 https://hub-downloads.confluent.io/api/plugins/confluentinc/kafka-connect-avro-converter/versions/8.3.2/confluentinc-kafka-connect-avro-converter-8.3.2.zip /in/avro.zip
RUN python -m zipfile -e /in/sink.zip /out \
    && python -m zipfile -e /in/avro.zip /out \
    && sha256sum /in/sink.zip | cut -d ' ' -f 1 > /out/iceberg-sink-zip.sha256

# 3. the Debezium image with both plugins under /kafka/connect, which is on its plugin path. The
#    sink zip's hash sits outside that path so the evidence can read it from the image.
FROM quay.io/debezium/connect:3.6.3.Final@sha256:665fef453613d9cfca4b1ec5dccfa3ba0e6bf4db8f39d7e853a848657e0ec7fa
COPY --from=unpack --chown=kafka:kafka /out/iceberg-kafka-connect-runtime-1.11.0 /kafka/connect/iceberg-kafka-connect
COPY --from=unpack --chown=kafka:kafka /out/confluentinc-kafka-connect-avro-converter-8.3.2 /kafka/connect/confluentinc-kafka-connect-avro-converter
COPY --from=unpack --chown=kafka:kafka /out/iceberg-sink-zip.sha256 /kafka/iceberg-sink-zip.sha256
