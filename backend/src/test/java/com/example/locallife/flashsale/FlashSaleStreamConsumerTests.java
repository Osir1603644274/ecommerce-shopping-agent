package com.example.locallife.flashsale;

import com.fasterxml.jackson.databind.ObjectMapper;
import org.junit.jupiter.api.Test;
import org.springframework.data.redis.connection.stream.*;
import org.springframework.data.redis.core.StreamOperations;
import org.springframework.data.redis.core.StringRedisTemplate;
import java.time.Duration;
import java.util.Map;
import static org.assertj.core.api.Assertions.*;
import static org.mockito.Mockito.*;
import static org.mockito.ArgumentMatchers.*;

class FlashSaleStreamConsumerTests {
    private final StringRedisTemplate redis = mock(StringRedisTemplate.class);
    @SuppressWarnings("unchecked")
    private final StreamOperations<String,Object,Object> stream = mock(StreamOperations.class);
    private final FlashSaleOrderPersistenceService orders = mock(FlashSaleOrderPersistenceService.class);
    private final FlashSaleRedisGateway gateway = mock(FlashSaleRedisGateway.class);
    private final FlashSaleStreamConsumer consumer = new FlashSaleStreamConsumer(redis,
            new FlashSaleProperties(true,"stream.test","group",Duration.ofSeconds(1),8),
            orders,gateway,new ObjectMapper(),"test");

    private MapRecord<String,Object,Object> record(Map<Object,Object> fields) {
        when(redis.<Object,Object>opsForStream()).thenReturn(stream);
        return StreamRecords.newRecord().in("stream.test").ofMap(fields).withId(RecordId.of("1-0"));
    }

    @Test
    void ackFailureAfterCommitNeverCompensatesEvenAtRetryLimit() {
        var message=record(Map.of("orderId","o1","campaignId","1","userId","u1","amountMinor","10"));
        when(stream.acknowledge(anyString(),anyString(),any(RecordId.class)))
                .thenThrow(new IllegalStateException("Redis disconnected after commit"));
        assertThatThrownBy(() -> consumer.processRecord(message,8)).isInstanceOf(IllegalStateException.class);
        verify(orders).persist("o1",1L,"u1",10L,"1-0");
        verify(orders,never()).deadLetterOrder(anyString(),anyString(),anyInt(),any(),anyString(),anyLong(),anyString());
        verifyNoInteractions(gateway);
    }

    @Test
    void committedOrderFoundAtFailureBoundaryDoesNotReleaseStock() {
        var message=record(Map.of("orderId","o1","campaignId","1","userId","u1","amountMinor","10"));
        when(orders.persist(anyString(),anyLong(),anyString(),anyLong(),anyString())).thenThrow(new IllegalStateException());
        when(orders.deadLetterOrder(anyString(),anyString(),anyInt(),any(),anyString(),anyLong(),anyString())).thenReturn(false);
        consumer.processRecord(message,8);
        verifyNoInteractions(gateway);
        verify(stream).acknowledge("stream.test","group",RecordId.of("1-0"));
    }

    @Test
    void malformedIdentityIsDurablyQuarantinedWithoutCompensation() {
        consumer.processRecord(record(Map.of("orderId","o1","campaignId","not-a-number")),1);
        verify(orders).deadLetter(eq("1-0"),contains("not-a-number"),eq(1),any());
        verify(stream).acknowledge("stream.test","group",RecordId.of("1-0"));
        verify(stream).delete("stream.test",RecordId.of("1-0"));
        verifyNoInteractions(gateway);
    }

    @Test
    void deadLetterStorageFailureLeavesPoisonMessageUnacknowledged() {
        var message=record(Map.of("campaignId","bad"));
        doThrow(new IllegalStateException("database down")).when(orders).deadLetter(anyString(),anyString(),anyInt(),any());
        assertThatThrownBy(() -> consumer.processRecord(message,8)).isInstanceOf(IllegalStateException.class);
        verifyNoInteractions(stream,gateway);
    }
}
