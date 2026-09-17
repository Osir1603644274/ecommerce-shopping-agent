package com.example.locallife.flashsale;

import com.fasterxml.jackson.databind.ObjectMapper;
import jakarta.annotation.PreDestroy;
import org.apache.rocketmq.client.consumer.DefaultMQPushConsumer;
import org.apache.rocketmq.client.consumer.listener.ConsumeConcurrentlyStatus;
import org.apache.rocketmq.client.consumer.listener.MessageListenerConcurrently;
import org.apache.rocketmq.client.producer.DefaultMQProducer;
import org.apache.rocketmq.client.producer.SendStatus;
import org.apache.rocketmq.common.consumer.ConsumeFromWhere;
import org.apache.rocketmq.common.message.Message;
import org.apache.rocketmq.common.message.MessageExt;
import org.springframework.beans.factory.ObjectProvider;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.boot.autoconfigure.condition.ConditionalOnExpression;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Component;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import java.nio.charset.StandardCharsets;
import java.util.UUID;

@Component
@ConditionalOnExpression("${local-life.flash-sale.enabled:true} and '${local-life.flash-sale.transport:rocketmq}' == 'rocketmq'")
public class FlashSaleRocketMq {
    private static final Logger log = LoggerFactory.getLogger(FlashSaleRocketMq.class);
    private final JdbcTemplate jdbc;
    private final ObjectMapper json;
    private final FlashSaleOrderPersistenceService orders;
    private final FlashSaleRedisGateway redis;
    private final ObjectProvider<FlashSaleFaultProbe> probes;
    private final String address, topic, group;
    private final int attempts;
    private DefaultMQProducer producer;
    private DefaultMQPushConsumer consumer, deadConsumer;

    public FlashSaleRocketMq(JdbcTemplate jdbc, ObjectMapper json, FlashSaleOrderPersistenceService orders,
            FlashSaleRedisGateway redis, ObjectProvider<FlashSaleFaultProbe> probes, FlashSaleProperties properties,
            @Value("${local-life.flash-sale.rocketmq.nameserver:localhost:9876}") String address,
            @Value("${local-life.flash-sale.rocketmq.topic:flash-sale-orders-v1}") String topic) {
        this.jdbc=jdbc; this.json=json; this.orders=orders; this.redis=redis; this.probes=probes;
        this.address=address; this.topic=topic; this.group=properties.consumerGroup(); this.attempts=properties.maxAttempts();
    }

    // Retry initialization rather than making a transient broker outage permanently disable consumption.
    private synchronized void ensureStarted() throws Exception {
        if (producer == null) {
            var candidate = new DefaultMQProducer(group + "-producer");
            candidate.setNamesrvAddr(address); candidate.setInstanceName(UUID.randomUUID().toString());
            candidate.setSendMsgTimeout(3000); candidate.setRetryTimesWhenSendFailed(0);
            candidate.setVipChannelEnabled(false);
            try { candidate.start(); producer=candidate; } catch (Exception e) { candidate.shutdown(); throw e; }
        }
        if (consumer == null) consumer=startConsumer(group,topic,false);
        if (deadConsumer == null) deadConsumer=startConsumer(group+"-dead-letter-archive","%DLQ%"+group,true);
    }

    private DefaultMQPushConsumer startConsumer(String name, String subscription, boolean dead) throws Exception {
        var candidate = new DefaultMQPushConsumer(name);
        candidate.setNamesrvAddr(address); candidate.setInstanceName(UUID.randomUUID().toString());
        candidate.setConsumeFromWhere(ConsumeFromWhere.CONSUME_FROM_FIRST_OFFSET);
        candidate.setConsumeMessageBatchMaxSize(1); candidate.setConsumeThreadMin(1); candidate.setConsumeThreadMax(2);
        candidate.setMaxReconsumeTimes(attempts-1); candidate.setPersistConsumerOffsetInterval(1000);
        candidate.setVipChannelEnabled(false); candidate.subscribe(subscription,"*");
        candidate.registerMessageListener((MessageListenerConcurrently) (messages, context) -> {
            context.setDelayLevelWhenNextConsume(1);
            try {
                for (var message : messages) consume(message,dead);
                return ConsumeConcurrentlyStatus.CONSUME_SUCCESS;
            } catch (Exception failure) {
                log.warn("RocketMQ delivery pending, deadLetter={}",dead,failure);
                return ConsumeConcurrentlyStatus.RECONSUME_LATER;
            }
        });
        try { candidate.start(); return candidate; } catch (Exception e) { candidate.shutdown(); throw e; }
    }

    @Scheduled(fixedDelayString="${local-life.flash-sale.dispatch-delay:PT1S}")
    public void dispatch() {
        try {
            ensureStarted();
            var pending = jdbc.query("""
                SELECT id,campaign_id,user_id,amount_minor FROM flash_sale_request
                WHERE status='PENDING' AND mq_published_at IS NULL AND mq_next_attempt_at<=CURRENT_TIMESTAMP
                ORDER BY mq_next_attempt_at,created_at LIMIT 100
                """, (rs,n)->new Command(rs.getString(1),rs.getLong(2),rs.getString(3),rs.getLong(4)));
            for (var command : pending) {
                try {
                    probes.orderedStream().forEach(p->p.at("before-send",command.orderId()));
                    send(command);
                    jdbc.update("UPDATE flash_sale_request SET mq_published_at=CURRENT_TIMESTAMP,mq_attempts=mq_attempts+1 WHERE id=?",command.orderId());
                } catch (Exception failure) {
                    jdbc.update("UPDATE flash_sale_request SET mq_attempts=mq_attempts+1,mq_next_attempt_at=DATE_ADD(CURRENT_TIMESTAMP,INTERVAL 5 SECOND) WHERE id=?",command.orderId());
                    log.warn("Durable flash-sale send will retry: {}",command.orderId(),failure);
                }
            }
        } catch (Exception failure) { log.warn("RocketMQ dispatcher will retry",failure); }
    }

    public void send(Command command) throws Exception {
        ensureStarted();
        var message = new Message(topic, json.writeValueAsBytes(command));
        message.setKeys(command.orderId());
        if (producer.send(message).getSendStatus()!=SendStatus.SEND_OK)
            throw new IllegalStateException("Broker did not confirm durable send");
    }

    private void consume(MessageExt message, boolean dead) throws Exception {
        Command command;
        String payload = new String(message.getBody(),StandardCharsets.UTF_8);
        try {
            command=json.readValue(payload,Command.class);
            if(command==null || command.orderId()==null) throw new IllegalArgumentException("Missing command identity");
            var accepted=jdbc.query("SELECT campaign_id,user_id,amount_minor FROM flash_sale_request WHERE id=?",
                    (rs,n)->new Command(command.orderId(),rs.getLong(1),rs.getString(2),rs.getLong(3)),command.orderId());
            if (accepted.size()!=1 || !command.equals(accepted.get(0)))
                throw new IllegalArgumentException("Message does not match durable acceptance");
        } catch (com.fasterxml.jackson.core.JsonProcessingException | IllegalArgumentException malformed) {
            // Archive an opaque body as a JSON string; never compensate an untrusted identity.
            orders.deadLetter(message.getMsgId(),json.writeValueAsString(payload),message.getReconsumeTimes()+1,malformed);
            return;
        }
        if (dead) {
            boolean compensate=orders.deadLetterOrder("rocketmq:"+command.orderId(),json.writeValueAsString(command),
                    message.getReconsumeTimes()+1,new IllegalStateException("RocketMQ retry budget exhausted"),
                    command.orderId(),command.campaignId(),command.userId());
            if (compensate) {
                redis.compensate(command.campaignId(),command.userId(),command.orderId());
                orders.compensationCompleted(command.orderId());
            }
        } else {
            probes.orderedStream().forEach(p->p.at("before-consume",command.orderId()));
            orders.persist(command.orderId(),command.campaignId(),command.userId(),command.amountMinor(),message.getMsgId());
            probes.orderedStream().forEach(p->p.at("after-commit-before-ack",command.orderId()));
        }
    }

    @PreDestroy public synchronized void close() {
        if(consumer!=null) consumer.shutdown(); if(deadConsumer!=null) deadConsumer.shutdown();
        if(producer!=null) producer.shutdown();
    }
    public record Command(String orderId,long campaignId,String userId,long amountMinor) { }
}
