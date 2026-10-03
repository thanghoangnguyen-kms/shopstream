#!/usr/bin/env bash
# The kafka-init one-shot's body. The brokers refuse auto-creation, so every topic the phase uses is
# created here at replication factor 3 and min.insync.replicas=2. Idempotent through --if-not-exists.
# The CDC topics use cleanup.policy=delete: compaction would make bronze a legitimate superset of
# the Kafka set. ADR-002 owns the real policy.
# The bootstrap list, replication factor and min ISR are parameters only for ADR-001's one-broker
# fallback (FALL-03); the defaults are the three-broker values, so nothing changes when they are unset.
set -euo pipefail

BOOTSTRAP="${KAFKA_BOOTSTRAP:-kafka-1:19092,kafka-2:19092,kafka-3:19092}"
REPLICATION_FACTOR="${TOPIC_REPLICATION_FACTOR:-3}"
MIN_ISR="${TOPIC_MIN_INSYNC_REPLICAS:-2}"
TOPICS=/opt/kafka/bin/kafka-topics.sh

# create NAME PARTITIONS [extra --config pairs, e.g. cleanup.policy=compact ...]
create() {
  local name="$1" partitions="$2"
  shift 2
  local args=()
  local extra
  for extra in "$@"; do
    args+=(--config "$extra")
  done
  "$TOPICS" --bootstrap-server "$BOOTSTRAP" --create --if-not-exists \
    --topic "$name" --partitions "$partitions" --replication-factor "$REPLICATION_FACTOR" \
    --config "min.insync.replicas=$MIN_ISR" ${args[@]+"${args[@]}"}
}

create shopstream.public.customers 3 cleanup.policy=delete
create shopstream.public.products 3 cleanup.policy=delete
create shopstream.public.orders 3 cleanup.policy=delete
create shopstream.public.order_items 3 cleanup.policy=delete
create shopstream.public.reviews 3 cleanup.policy=delete
create control-iceberg 1
create __debezium-heartbeat.shopstream 1
create fx.refresh 1
create clickstream 6 cleanup.policy=delete
create clickstream.dlq 1
create control-iceberg-clicks 1
create connect-configs 1 cleanup.policy=compact
create connect-offsets 5 cleanup.policy=compact
create connect-status 5 cleanup.policy=compact
create _schemas 1 cleanup.policy=compact

"$TOPICS" --bootstrap-server "$BOOTSTRAP" --describe
