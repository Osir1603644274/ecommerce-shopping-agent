package com.example.locallife.product;

import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.boot.test.autoconfigure.web.servlet.AutoConfigureMockMvc;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.transaction.annotation.Transactional;
import static org.springframework.security.test.web.servlet.request.SecurityMockMvcRequestPostProcessors.jwt;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.*;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.*;
import static org.hamcrest.Matchers.hasSize;

@SpringBootTest
@AutoConfigureMockMvc
@Transactional
class ProductFavoriteTests {
    @Autowired MockMvc mvc;
    @Test void anonymousReadsAndWritesAreDenied() throws Exception {
        mvc.perform(get("/api/product-favorites")).andExpect(status().isUnauthorized());
        mvc.perform(put("/api/product-favorites/1001")).andExpect(status().isUnauthorized());
    }
    @Test void favoritesAreIdempotentAndOwnerIsolated() throws Exception {
        var alice=jwt().jwt(j -> j.subject("favorite-alice"));
        var bob=jwt().jwt(j -> j.subject("favorite-bob"));
        mvc.perform(put("/api/product-favorites/1001").with(alice)).andExpect(status().isOk());
        mvc.perform(put("/api/product-favorites/1001").with(alice)).andExpect(status().isOk());
        mvc.perform(get("/api/product-favorites").with(alice)).andExpect(jsonPath("$.data",hasSize(1)))
                .andExpect(jsonPath("$.data[0].id").value(1001));
        mvc.perform(get("/api/product-favorites").with(bob)).andExpect(jsonPath("$.data",hasSize(0)));
        mvc.perform(delete("/api/product-favorites/1001").with(bob)).andExpect(status().isOk());
        mvc.perform(get("/api/product-favorites").with(alice)).andExpect(jsonPath("$.data",hasSize(1)));
        mvc.perform(delete("/api/product-favorites/1001").with(alice)).andExpect(status().isOk());
        mvc.perform(get("/api/product-favorites").with(alice)).andExpect(jsonPath("$.data",hasSize(0)));
    }
    @Test void nonexistentProductCannotBeSaved() throws Exception {
        mvc.perform(put("/api/product-favorites/99999999").with(jwt())).andExpect(status().isNotFound());
    }
}
