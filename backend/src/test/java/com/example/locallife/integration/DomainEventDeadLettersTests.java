package com.example.locallife.integration;

import com.fasterxml.jackson.databind.ObjectMapper;
import org.junit.jupiter.api.Test;
import org.springframework.jdbc.core.JdbcTemplate;
import java.time.Duration;
import static org.assertj.core.api.Assertions.*;
import static org.mockito.Mockito.*;
import static org.mockito.ArgumentMatchers.*;

class DomainEventDeadLettersTests {
    @Test
    void preservesRawMalformedDeliveryAndPropagatesStorageFailure() {
        var jdbc=mock(JdbcTemplate.class);
        var properties=new MessagingProperties(true,"kafka","events",null,"group","consumer",8,
                Duration.ofSeconds(1),Duration.ofMinutes(5));
        var letters=new DomainEventDeadLetters(jdbc,new ObjectMapper(),properties);
        letters.record("events",1,42,"order-1","{broken",new IllegalArgumentException());
        verify(jdbc).update(contains("DOMAIN_KAFKA"),anyString(),contains("{broken"),eq("IllegalArgumentException"));
        when(jdbc.update(anyString(),anyString(),anyString(),anyString())).thenThrow(new IllegalStateException("database unavailable"));
        assertThatThrownBy(() -> letters.record("events",1,42,"order-1","{broken",new IllegalArgumentException()))
                .isInstanceOf(IllegalStateException.class);
    }

    @Test
    void retiredRedisTransportFailsBeforeAnyConsumptionOrPublishing() {
        assertThatThrownBy(() -> new MessagingProperties(true,"redis-stream",null,null,null,null,8,null,null))
                .isInstanceOf(IllegalArgumentException.class).hasMessageContaining("drain/export");
    }
}
