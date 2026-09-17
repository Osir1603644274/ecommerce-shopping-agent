package com.example.locallife.integration;

import org.junit.jupiter.api.Test;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.mockito.Mockito.*;
import static org.mockito.ArgumentMatchers.*;
import org.apache.kafka.clients.consumer.Consumer;
import org.apache.kafka.clients.consumer.ConsumerRecord;
import org.springframework.kafka.listener.ContainerProperties;
import org.springframework.kafka.listener.MessageListenerContainer;
import org.springframework.util.backoff.FixedBackOff;

class KafkaFailureRecoveryConfigurationTests {

    @Test
    void configuresARealErrorHandlerInsteadOfImmediateTightLoopRetries() {
        assertThat(new KafkaFailureRecoveryConfiguration()
                .domainEventKafkaErrorHandler(org.mockito.Mockito.mock(DomainEventDeadLetters.class))).isNotNull();
    }

    @Test
    @SuppressWarnings("unchecked")
    void brokerOffsetCommitsOnlyAfterDurableRecoveryAndRemainsUncommittedOnDatabaseFailure() {
        var deadLetters=mock(DomainEventDeadLetters.class);
        var handler=new KafkaFailureRecoveryConfiguration().domainEventKafkaErrorHandler(deadLetters);
        handler.setBackOffFunction((record,failure)->new FixedBackOff(0,0));
        Consumer<Object,Object> consumer=mock(Consumer.class);
        var container=mock(MessageListenerContainer.class);
        var properties=new ContainerProperties("topic");
        properties.setAckMode(ContainerProperties.AckMode.MANUAL_IMMEDIATE);
        when(container.getContainerProperties()).thenReturn(properties);
        when(container.isRunning()).thenReturn(true);
        var record=new ConsumerRecord<Object,Object>("topic",0,7,"key","malformed");
        var failure=new IllegalArgumentException("bad envelope");
        doThrow(new org.springframework.dao.DataAccessResourceFailureException("database offline"))
                .when(deadLetters).record(anyString(),anyInt(),anyLong(),any(),any(),any());
        assertThatThrownBy(()->handler.handleRemaining(failure,java.util.List.of(record),consumer,container))
                .isInstanceOf(RuntimeException.class);
        verify(consumer,never()).commitSync(anyMap(),any());
        verify(consumer,never()).commitAsync(anyMap(),any());
        doNothing().when(deadLetters).record(anyString(),anyInt(),anyLong(),any(),any(),any());
        handler.handleRemaining(failure,java.util.List.of(record),consumer,container);
        var order=inOrder(deadLetters,consumer);
        order.verify(deadLetters,atLeastOnce()).record(eq("topic"),eq(0),eq(7L),eq("key"),eq("malformed"),any());
        order.verify(consumer).commitSync(argThat(offsets->offsets.values().iterator().next().offset()==8L),any());
    }
}
