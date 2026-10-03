"""ADR-001 item 6: a message on the fx.refresh topic starts this DAG, through an AssetWatcher.

The asset `fx_refresh_requests` carries one watcher, which runs a MessageQueueTrigger on the Kafka
topic `fx.refresh`. Each message becomes one asset event and one asset-triggered run. The apply
function is named by a dotted-path string because the triggerer imports it from the plugins
folder (see plugins/kafka_apply.py). The Kafka connection `kafka_default` comes from the
environment, so this file holds no secret and no broker address.

The fallback if the go criterion fails is a 5-minute schedule on the same DAG.
"""

from airflow.providers.common.messaging.triggers.msg_queue import MessageQueueTrigger
from airflow.providers.standard.operators.empty import EmptyOperator
from airflow.sdk import DAG, Asset, AssetWatcher

trigger = MessageQueueTrigger(
    scheme="kafka",
    topics=["fx.refresh"],
    apply_function="kafka_apply.apply_function",
    kafka_config_id="kafka_default",
    poll_timeout=1,
    poll_interval=5,
)
asset = Asset(
    "fx_refresh_requests",
    watchers=[AssetWatcher(name="fx_kafka_watcher", trigger=trigger)],
)

with DAG(dag_id="fx_refresh_on_message", schedule=[asset], catchup=False):
    EmptyOperator(task_id="refresh")
