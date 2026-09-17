package com.example.locallife.flashsale;

import com.fasterxml.jackson.databind.ObjectMapper;
import org.junit.jupiter.api.Test;
import java.time.Duration;
import java.util.List;
import static org.mockito.Mockito.*;
import static org.mockito.ArgumentMatchers.*;

class FlashSaleRequestRecoveryTests {
    @Test void unavailableRedisBacksOffCompensationAndStillRecoversTheNextOrder() {
        var requests=mock(FlashSaleRequestStore.class);
        var orders=mock(FlashSaleOrderPersistenceService.class);
        var redis=mock(FlashSaleRedisGateway.class);
        when(requests.pending()).thenReturn(List.of(
                new FlashSaleRequestStore.Request("failed",1,"u1",10,8,"COMPENSATING"),
                new FlashSaleRequestStore.Request("next",1,"u2",10,0,"PENDING")));
        doThrow(new IllegalStateException("redis offline")).when(redis).compensate(1L,"u1","failed");
        when(orders.deadLetterOrder(anyString(),anyString(),anyInt(),any(),anyString(),anyLong(),anyString())).thenReturn(true);
        new FlashSaleRequestRecovery(requests,orders,redis,
                new FlashSaleProperties(true,"stream","group",Duration.ofSeconds(1),8),new ObjectMapper()).recover();
        verify(requests).retry(eq("failed"),contains("redis offline"));
        verify(orders).persist("next",1L,"u2",10L,"recovery:next");
        verify(orders,never()).compensationCompleted("failed");
    }
}
