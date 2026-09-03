package com.example.locallife.product;

import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.autoconfigure.web.servlet.AutoConfigureMockMvc;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.http.MediaType;
import org.springframework.test.web.servlet.MockMvc;

import static org.hamcrest.Matchers.hasSize;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

@SpringBootTest
@AutoConfigureMockMvc
class ProductControllerTests {
    @Autowired
    private MockMvc mockMvc;

    @Test
    void listsAndFiltersProducts() throws Exception {
        mockMvc.perform(get("/api/products").param("category", "手机"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.data", hasSize(1)))
                .andExpect(jsonPath("$.data[0].id").value(1001));
    }

    @Test
    void priceFilterUsesOnlyVerifiedSnapshots() throws Exception {
        mockMvc.perform(get("/api/products").param("maxPriceMinor", "1000000"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.data", hasSize(2)))
                .andExpect(jsonPath("$.data[?(@.id == 1002)]").isEmpty());
    }

    @Test
    void returnsDetailWithEvidenceAttributes() throws Exception {
        mockMvc.perform(get("/api/products/1001"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.data.sourceItemId").value("fixture-phone-1"))
                .andExpect(jsonPath("$.data.attributes", hasSize(4)))
                .andExpect(jsonPath("$.data.attributes[0].evidenceField").isNotEmpty());
    }

    @Test
    void retrievalReportsAuthoritativeFallbackChannel() throws Exception {
        mockMvc.perform(get("/api/products/retrieval")
                        .param("query", "手机")
                        .param("category", "手机"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.data.channel").value("mysql"))
                .andExpect(jsonPath("$.data.products", hasSize(1)));
    }

    @Test
    void resolvesAtMostTenProducts() throws Exception {
        mockMvc.perform(post("/api/products/resolve")
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("{\"productIds\":[1003,1001,9999]}"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.data", hasSize(2)))
                .andExpect(jsonPath("$.data[0].id").value(1003));
    }

    @Test
    void rejectsOversizedResolveRequest() throws Exception {
        mockMvc.perform(post("/api/products/resolve")
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("{\"productIds\":[1,2,3,4,5,6,7,8,9,10,11]}"))
                .andExpect(status().isBadRequest());
    }
}
