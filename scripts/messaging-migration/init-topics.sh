#!/bin/sh
set -eu
# Preserve existing topology: shrinking a topic can hide queued messages.
ensure_topic() {
    topic="$1"
    default_queues="$2"
    route=$(sh mqadmin topicRoute -n nameserver:9876 -t "$topic" 2>/dev/null || true)
    reads=$(printf '%s\n' "$route" | sed -n 's/.*"readQueueNums"[[:space:]]*:[[:space:]]*\([0-9][0-9]*\).*/\1/p' | sort -nr | head -n 1)
    writes=$(printf '%s\n' "$route" | sed -n 's/.*"writeQueueNums"[[:space:]]*:[[:space:]]*\([0-9][0-9]*\).*/\1/p' | sort -nr | head -n 1)
    # DLQ requires read permission for the archiver; preserve its queue counts too.
    until sh mqadmin updateTopic -n nameserver:9876 -b rocketmq:10911 -t "$topic" -r "${reads:-$default_queues}" -w "${writes:-$default_queues}" -p 6; do sleep 2; done
}
ensure_topic "${ROCKETMQ_FLASH_SALE_TOPIC:-flash-sale-orders-v1}" 4
ensure_topic "%DLQ%${FLASH_SALE_CONSUMER_GROUP:-flash-sale-order-group}" 1
