package com.example.locallife.shop;

import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.autoconfigure.web.servlet.AutoConfigureMockMvc;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.http.MediaType;
import org.springframework.security.test.context.support.WithMockUser;
import org.springframework.test.context.jdbc.Sql;
import org.springframework.test.web.servlet.MockMvc;

import static org.hamcrest.Matchers.hasSize;
import static org.hamcrest.Matchers.lessThan;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.put;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

@SpringBootTest
@AutoConfigureMockMvc
@WithMockUser(roles = "ADMIN")
class ShopControllerTests {

    @Autowired
    private MockMvc mockMvc;

    @Test
    void listShopsReturnsAllShops() throws Exception {
        mockMvc.perform(get("/api/shops"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.success").value(true))
                .andExpect(jsonPath("$.data", hasSize(15)))
                .andExpect(jsonPath("$.data[0].id").value(1));
    }

    @Test
    void listShopsCanFilterByTypeId() throws Exception {
        mockMvc.perform(get("/api/shops").param("typeId", "2"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.success").value(true))
                .andExpect(jsonPath("$.data", hasSize(3)))
                .andExpect(jsonPath("$.data[0].typeId").value(2));
    }

    @Test
    void listShopsCanFilterByName() throws Exception {
        mockMvc.perform(get("/api/shops").param("name", "手冲"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.success").value(true))
                .andExpect(jsonPath("$.data", hasSize(1)))
                .andExpect(jsonPath("$.data[0].id").value(3))
                .andExpect(jsonPath("$.data[0].name").value("清晨手冲咖啡"));
    }

    @Test
    void listShopsCanCombineTypeAndNameFilters() throws Exception {
        mockMvc.perform(get("/api/shops")
                        .param("typeId", "2")
                        .param("name", "书屋"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.data", hasSize(1)))
                .andExpect(jsonPath("$.data[0].id").value(7));
    }

    @Test
    void getShopReturnsOneShop() throws Exception {
        mockMvc.perform(get("/api/shops/3"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.success").value(true))
                .andExpect(jsonPath("$.data.id").value(3))
                .andExpect(jsonPath("$.data.avgPrice").value(35))
                .andExpect(jsonPath("$.data.phone").value("010-8888-0003"))
                .andExpect(jsonPath("$.data.createdAt").doesNotExist())
                .andExpect(jsonPath("$.data.updatedAt").doesNotExist());
    }

    @Test
    void listNearbyShopsReturnsShopsSortedByDistance() throws Exception {
        mockMvc.perform(get("/api/shops/nearby")
                        .param("typeId", "2")
                        .param("longitude", "116.4000")
                        .param("latitude", "39.9000")
                        .param("radiusMeters", "500")
                        .param("limit", "10"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.success").value(true))
                .andExpect(jsonPath("$.data", hasSize(2)))
                .andExpect(jsonPath("$.data[0].id").value(3))
                .andExpect(jsonPath("$.data[0].distanceMeters").value(0.0))
                .andExpect(jsonPath("$.data[0].longitude").value(116.4000))
                .andExpect(jsonPath("$.data[0].latitude").value(39.9000))
                .andExpect(jsonPath("$.data[1].id").value(7))
                .andExpect(jsonPath("$.data[1].distanceMeters").value(lessThan(250.0)));
    }

    @Test
    void getShopReturns404WhenNotFound() throws Exception {
        mockMvc.perform(get("/api/shops/999"))
                .andExpect(status().isNotFound())
                .andExpect(jsonPath("$.success").value(false))
                .andExpect(jsonPath("$.message").value("商户不存在"));
    }

    @Test
    @Sql(
            statements = "UPDATE shop SET address = '文化广场 3 号', avg_price = 35, phone = '010-8888-0003' WHERE id = 3",
            executionPhase = Sql.ExecutionPhase.AFTER_TEST_METHOD
    )
    void updateShopReturnsUpdatedDetail() throws Exception {
        mockMvc.perform(put("/api/shops/3")
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("""
                                {
                                  "address": "Culture Plaza North Gate",
                                  "avgPrice": 39,
                                  "phone": "010-8888-9999"
                                }
                                """))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.success").value(true))
                .andExpect(jsonPath("$.data.id").value(3))
                .andExpect(jsonPath("$.data.address").value("Culture Plaza North Gate"))
                .andExpect(jsonPath("$.data.avgPrice").value(39))
                .andExpect(jsonPath("$.data.phone").value("010-8888-9999"));
    }

    @Test
    void updateShopReturns404WhenShopDoesNotExist() throws Exception {
        mockMvc.perform(put("/api/shops/999")
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("""
                                {
                                  "address": "不存在地址",
                                  "avgPrice": 39,
                                  "phone": "010-8888-9999"
                                }
                                """))
                .andExpect(status().isNotFound())
                .andExpect(jsonPath("$.success").value(false))
                .andExpect(jsonPath("$.message").value("商户不存在"));
    }
}
