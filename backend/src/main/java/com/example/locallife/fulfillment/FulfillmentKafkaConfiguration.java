package com.example.locallife.fulfillment;

import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.kafka.config.ConcurrentKafkaListenerContainerFactory;
import org.springframework.kafka.core.ConsumerFactory;
import org.springframework.kafka.listener.ContainerProperties;
import org.springframework.kafka.listener.DefaultErrorHandler;
import org.springframework.util.backoff.FixedBackOff;

@Configuration
@ConditionalOnProperty(prefix = "local-life.fulfillment", name = "kafka-enabled", havingValue = "true")
class FulfillmentKafkaConfiguration {
    @Bean
    ConcurrentKafkaListenerContainerFactory<Object, Object> fulfillmentKafkaListenerContainerFactory(
            ConsumerFactory<Object, Object> consumerFactory, FulfillmentDeadLetters deadLetters,
            org.springframework.boot.autoconfigure.kafka.ConcurrentKafkaListenerContainerFactoryConfigurer configurer) {
        var factory = new ConcurrentKafkaListenerContainerFactory<Object, Object>();
        configurer.configure(factory, consumerFactory);
        factory.getContainerProperties().setAckMode(ContainerProperties.AckMode.MANUAL_IMMEDIATE);
        // Broker offsets can advance only after either business commit or a durable, replayable dead letter.
        var errors = new DefaultErrorHandler((record, failure) -> deadLetters.record(record.topic(),
                record.partition(), record.offset(), String.valueOf(record.value()), failure), new FixedBackOff(1000, 3));
        errors.setCommitRecovered(true);
        errors.setClassifications(java.util.Map.of(Exception.class, true), true);
        factory.setCommonErrorHandler(errors);
        return factory;
    }
}
