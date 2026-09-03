package com.example.locallife.shop;

import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.autoconfigure.web.servlet.AutoConfigureMockMvc;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.test.web.servlet.MockMvc;

import static org.hamcrest.Matchers.hasSize;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

@SpringBootTest
@AutoConfigureMockMvc
class ShopTypeControllerTests {

    @Autowired
    private MockMvc mockMvc;

    @Test
    void listShopTypesReturnsAllShopTypes() throws Exception {
        mockMvc.perform(get("/api/shop-types"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.success").value(true))
                .andExpect(jsonPath("$.data", hasSize(5)))
                .andExpect(jsonPath("$.data[0].name").value("美食"));
    }

    @Test
    void listShopTypesCanFilterByKeyword() throws Exception {
        mockMvc.perform(get("/api/shop-types").param("keyword", "咖啡"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.success").value(true))
                .andExpect(jsonPath("$.data", hasSize(1)))
                .andExpect(jsonPath("$.data[0].name").value("咖啡"));
    }

    @Test
    void getShopTypeReturnsOneShopType() throws Exception {
        mockMvc.perform(get("/api/shop-types/2"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.success").value(true))
                .andExpect(jsonPath("$.data.id").value(2))
                .andExpect(jsonPath("$.data.name").value("咖啡"));
    }

    @Test
    void getShopTypeReturns404WhenNotFound() throws Exception {
        mockMvc.perform(get("/api/shop-types/999"))
                .andExpect(status().isNotFound())
                .andExpect(jsonPath("$.success").value(false))
                .andExpect(jsonPath("$.message").value("商户分类不存在"));
    }
}

