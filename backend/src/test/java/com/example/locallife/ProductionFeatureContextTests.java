package com.example.locallife;

import com.example.locallife.review.ReviewVectorSyncClient;
import org.junit.jupiter.api.Test;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.test.context.bean.override.mockito.MockitoBean;

@SpringBootTest(properties = {
        "local-life.rate-limit.enabled=true",
        "local-life.messaging.enabled=false",
        "local-life.flash-sale.enabled=false"
})
class ProductionFeatureContextTests {
    @MockitoBean
    private ReviewVectorSyncClient reviewVectorSyncClient;

    @Test
    void productionRateLimitBeansCanStart() {
    }
}
