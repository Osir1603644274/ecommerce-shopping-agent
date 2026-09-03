package com.example.locallife.catalogcontract;

import com.example.locallife.review.ReviewVectorSyncClient;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.autoconfigure.web.servlet.AutoConfigureMockMvc;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.test.context.bean.override.mockito.MockitoBean;
import org.springframework.test.web.servlet.MockMvc;

import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

@SpringBootTest(properties = {
        "local-life.deployment.role=catalog",
        "local-life.deployment.require-internal-token=true",
        "local-life.deployment.internal-token=0123456789abcdef0123456789abcdef"
})
@AutoConfigureMockMvc
class TradeCatalogInternalControllerTests {
    @Autowired
    private MockMvc mockMvc;

    @MockitoBean
    private ReviewVectorSyncClient reviewVectorSyncClient;

    @Test
    void returnsAuthoritativeTradeSnapshotOnlyWithInternalToken() throws Exception {
        mockMvc.perform(get("/internal/trade-catalog/items/PRODUCT/1001"))
                .andExpect(status().isForbidden());

        mockMvc.perform(get("/internal/trade-catalog/items/PRODUCT/1001")
                        .header("X-Internal-Service-Token",
                                "0123456789abcdef0123456789abcdef"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.success").value(true))
                .andExpect(jsonPath("$.data.itemType").value("PRODUCT"))
                .andExpect(jsonPath("$.data.unitPriceMinor").value(249900))
                .andExpect(jsonPath("$.data.entityVersion").isNumber());
    }
}
