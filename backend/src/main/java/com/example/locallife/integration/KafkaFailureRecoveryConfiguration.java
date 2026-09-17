package com.example.locallife.integration;

import org.springframework.boot.autoconfigure.condition.ConditionalOnExpression;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.kafka.listener.DefaultErrorHandler;
import org.springframework.util.backoff.ExponentialBackOff;
import org.springframework.util.backoff.FixedBackOff;

@Configuration
@ConditionalOnExpression(
        "${local-life.messaging.enabled:true}"
                + " and '${local-life.messaging.transport:kafka}' == 'kafka'"
)
class KafkaFailureRecoveryConfiguration {

    @Bean
    DefaultErrorHandler domainEventKafkaErrorHandler(DomainEventDeadLetters deadLetters) {
        ExponentialBackOff backOff = new ExponentialBackOff();
        backOff.setInitialInterval(250L);
        backOff.setMultiplier(2.0);
        backOff.setMaxInterval(5_000L);
        backOff.setMaxElapsedTime(30_000L);
        var handler = new DefaultErrorHandler((record, failure) -> deadLetters.record(
                record.topic(), record.partition(), record.offset(),
                record.key() == null ? null : record.key().toString(),
                record.value() == null ? null : record.value().toString(), failure), backOff);
        handler.setCommitRecovered(true);
        handler.setClassifications(java.util.Map.of(Exception.class, true), true);
        handler.setBackOffFunction((record, failure) -> {
            for (Throwable cause = failure; cause != null; cause = cause.getCause()) {
                if (cause instanceof InboxBusyException) {
                    return new FixedBackOff(1000L, FixedBackOff.UNLIMITED_ATTEMPTS);
                }
            }
            return backOff;
        });
        return handler;
    }
}
