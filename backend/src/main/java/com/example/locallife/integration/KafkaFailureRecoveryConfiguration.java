package com.example.locallife.integration;

import org.springframework.boot.autoconfigure.condition.ConditionalOnExpression;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.kafka.listener.DefaultErrorHandler;
import org.springframework.util.backoff.ExponentialBackOff;

@Configuration
@ConditionalOnExpression(
        "${local-life.messaging.enabled:true}"
                + " and '${local-life.messaging.transport:kafka}' == 'kafka'"
)
class KafkaFailureRecoveryConfiguration {

    @Bean
    DefaultErrorHandler domainEventKafkaErrorHandler() {
        ExponentialBackOff backOff = new ExponentialBackOff();
        backOff.setInitialInterval(250L);
        backOff.setMultiplier(2.0);
        backOff.setMaxInterval(5_000L);
        backOff.setMaxElapsedTime(30_000L);
        return new DefaultErrorHandler(backOff);
    }
}
