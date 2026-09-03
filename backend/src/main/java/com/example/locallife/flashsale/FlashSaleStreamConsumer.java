package com.example.locallife.flashsale;

import com.fasterxml.jackson.databind.ObjectMapper;
import jakarta.annotation.PostConstruct;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.data.domain.Range;
import org.springframework.data.redis.connection.stream.Consumer;
import org.springframework.data.redis.connection.stream.MapRecord;
import org.springframework.data.redis.connection.stream.PendingMessage;
import org.springframework.data.redis.connection.stream.PendingMessages;
import org.springframework.data.redis.connection.stream.ReadOffset;
import org.springframework.data.redis.connection.stream.RecordId;
import org.springframework.data.redis.connection.stream.StreamOffset;
import org.springframework.data.redis.connection.stream.StreamReadOptions;
import org.springframework.data.redis.core.StringRedisTemplate;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Component;

import java.time.Duration;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.stream.Collectors;

@Component
@ConditionalOnProperty(
        prefix = "local-life.flash-sale",
        name = "enabled",
        havingValue = "true"
)
class FlashSaleStreamConsumer {
    private static final Logger log = LoggerFactory.getLogger(FlashSaleStreamConsumer.class);
    private final StringRedisTemplate redisTemplate;
    private final FlashSaleProperties properties;
    private final FlashSaleOrderPersistenceService persistenceService;
    private final FlashSaleRedisGateway redisGateway;
    private final ObjectMapper objectMapper;
    private final String consumerName;

    FlashSaleStreamConsumer(
            StringRedisTemplate redisTemplate,
            FlashSaleProperties properties,
            FlashSaleOrderPersistenceService persistenceService,
            FlashSaleRedisGateway redisGateway,
            ObjectMapper objectMapper,
            @Value("${HOSTNAME:local}") String consumerName
    ) {
        this.redisTemplate = redisTemplate;
        this.properties = properties;
        this.persistenceService = persistenceService;
        this.redisGateway = redisGateway;
        this.objectMapper = objectMapper;
        this.consumerName = consumerName;
    }

    @PostConstruct
    void createGroup() {
        try {
            if (Boolean.FALSE.equals(redisTemplate.hasKey(properties.stream()))) {
                redisTemplate.opsForStream().add(
                        properties.stream(),
                        Map.of("bootstrap", "true")
                );
            }
            redisTemplate.opsForStream().createGroup(
                    properties.stream(),
                    ReadOffset.from("0-0"),
                    properties.consumerGroup()
            );
        } catch (RuntimeException exception) {
            if (!isBusyGroup(exception)) {
                log.warn("秒杀 Stream 消费组初始化失败，后续轮询将重试", exception);
            }
        }
    }

    @Scheduled(fixedDelayString = "${local-life.flash-sale.poll-delay:PT1S}")
    void poll() {
        try {
            recoverStalePending();
            List<MapRecord<String, Object, Object>> records =
                    redisTemplate.opsForStream().read(
                            Consumer.from(properties.consumerGroup(), consumerName),
                            StreamReadOptions.empty().count(20).block(Duration.ofMillis(500)),
                            StreamOffset.create(properties.stream(), ReadOffset.lastConsumed())
                    );
            process(records, 1L);
        } catch (RuntimeException exception) {
            log.warn("秒杀 Stream 消费失败，消息保留在 Pending List", exception);
            createGroup();
        }
    }

    private void recoverStalePending() {
        PendingMessages pending = redisTemplate.opsForStream().pending(
                properties.stream(),
                properties.consumerGroup(),
                Range.unbounded(),
                100
        );
        if (pending == null) {
            return;
        }
        List<PendingMessage> stale = pending.stream()
                .filter(message -> message.getElapsedTimeSinceLastDelivery()
                        .compareTo(properties.claimIdleAfter()) >= 0)
                .toList();
        if (stale.isEmpty()) {
            return;
        }
        RecordId[] ids = stale.stream()
                .map(PendingMessage::getId)
                .toArray(RecordId[]::new);
        List<MapRecord<String, Object, Object>> claimed =
                redisTemplate.opsForStream().claim(
                        properties.stream(),
                        properties.consumerGroup(),
                        consumerName,
                        properties.claimIdleAfter(),
                        ids
                );
        Map<String, Long> deliveryCounts = stale.stream().collect(Collectors.toMap(
                message -> message.getId().getValue(),
                message -> message.getTotalDeliveryCount() + 1L
        ));
        if (claimed != null) {
            for (MapRecord<String, Object, Object> record : claimed) {
                processRecord(
                        record,
                        deliveryCounts.getOrDefault(record.getId().getValue(), 1L)
                );
            }
        }
    }

    private void process(
            List<MapRecord<String, Object, Object>> records,
            long deliveryCount
    ) {
        if (records == null) {
            return;
        }
        for (MapRecord<String, Object, Object> record : records) {
            processRecord(record, deliveryCount);
        }
    }

    private void processRecord(
            MapRecord<String, Object, Object> record,
            long deliveryCount
    ) {
        Map<Object, Object> fields = record.getValue();
        if (!fields.containsKey("orderId")) {
            acknowledge(record);
            return;
        }
        Long campaignId = Long.valueOf(required(fields, "campaignId"));
        String userId = required(fields, "userId");
        try {
            persistenceService.persist(
                    required(fields, "orderId"),
                    campaignId,
                    userId,
                    Long.valueOf(required(fields, "amountMinor")),
                    record.getId().getValue()
            );
            acknowledge(record);
        } catch (RuntimeException exception) {
            if (deliveryCount < properties.maxAttempts()) {
                throw exception;
            }
            deadLetter(record, fields, campaignId, userId, deliveryCount, exception);
        }
    }

    private void deadLetter(
            MapRecord<String, Object, Object> record,
            Map<Object, Object> fields,
            Long campaignId,
            String userId,
            long deliveryCount,
            RuntimeException failure
    ) {
        try {
            Map<String, String> traceablePayload = new LinkedHashMap<>();
            traceablePayload.put("streamMessageId", record.getId().getValue());
            fields.forEach((key, value) ->
                    traceablePayload.put(String.valueOf(key), String.valueOf(value)));
            String payload = objectMapper.writeValueAsString(traceablePayload);
            persistenceService.deadLetter(
                    record.getId().getValue(),
                    payload,
                    Math.toIntExact(deliveryCount),
                    failure
            );
            redisGateway.compensate(campaignId, userId);
            acknowledge(record);
        } catch (Exception deadLetterFailure) {
            failure.addSuppressed(deadLetterFailure);
            throw failure;
        }
    }

    private void acknowledge(MapRecord<String, Object, Object> record) {
        redisTemplate.opsForStream().acknowledge(
                properties.stream(),
                properties.consumerGroup(),
                record.getId()
        );
    }

    private static String required(Map<Object, Object> fields, String field) {
        Object value = fields.get(field);
        if (value == null || value.toString().isBlank()) {
            throw new IllegalArgumentException("秒杀消息缺少字段: " + field);
        }
        return value.toString();
    }

    private static boolean isBusyGroup(RuntimeException exception) {
        return exception.getMessage() != null
                && exception.getMessage().contains("BUSYGROUP");
    }
}
