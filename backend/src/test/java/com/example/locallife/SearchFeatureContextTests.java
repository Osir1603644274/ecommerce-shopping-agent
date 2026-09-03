package com.example.locallife;

import com.example.locallife.review.ReviewVectorSyncClient;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.boot.test.autoconfigure.web.servlet.AutoConfigureMockMvc;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.test.context.bean.override.mockito.MockitoBean;

import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

@SpringBootTest(properties = {
        "local-life.search.enabled=true",
        "local-life.search.base-url=http://127.0.0.1:1",
        "local-life.search.initial-reconcile-delay=PT1H",
        "local-life.cache.shop.enabled=false",
        "local-life.cache.product.enabled=false",
        "local-life.messaging.enabled=false",
        "local-life.flash-sale.enabled=false",
        "local-life.rate-limit.enabled=false",
        "management.health.redis.enabled=false",
        "management.endpoint.health.status.order=down,out-of-service,degraded,unknown,up",
        "management.endpoint.health.status.http-mapping.degraded=200"
})
@AutoConfigureMockMvc
class SearchFeatureContextTests {
    @Autowired
    private MockMvc mockMvc;

    @MockitoBean
    private ReviewVectorSyncClient reviewVectorSyncClient;

    @Test
    void unavailableOptionalSearchReportsDegradedWithoutFailingReadiness() throws Exception {
        mockMvc.perform(get("/actuator/health"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.status").value("DEGRADED"));
    }
}
