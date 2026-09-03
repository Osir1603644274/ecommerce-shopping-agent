package com.example.locallife.behavior;

import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.autoconfigure.web.servlet.AutoConfigureMockMvc;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.http.MediaType;
import org.springframework.security.test.context.support.WithMockUser;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.transaction.annotation.Transactional;

import static org.hamcrest.Matchers.hasSize;
import static org.hamcrest.Matchers.startsWith;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

@SpringBootTest
@AutoConfigureMockMvc
@Transactional
@WithMockUser(username = "user-001")
class UserBehaviorControllerTests {

    @Autowired
    private MockMvc mockMvc;

    @Test
    void createBehaviorReturnsCreatedBehavior() throws Exception {
        mockMvc.perform(post("/api/user-behaviors")
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("""
                                {
                                  "userId": "user-001",
                                  "shopId": 3,
                                  "behaviorType": "favorite",
                                  "score": null,
                                  "source": "detail_page",
                                  "occurredAt": "2026-07-07T10:00:00"
                                }
                                """))
                .andExpect(status().isCreated())
                .andExpect(jsonPath("$.success").value(true))
                .andExpect(jsonPath("$.data.id", startsWith("behavior-")))
                .andExpect(jsonPath("$.data.userId").value("user-001"))
                .andExpect(jsonPath("$.data.shopId").value(3))
                .andExpect(jsonPath("$.data.behaviorType").value("favorite"))
                .andExpect(jsonPath("$.data.score").doesNotExist())
                .andExpect(jsonPath("$.data.source").value("detail_page"))
                .andExpect(jsonPath("$.data.occurredAt").value("2026-07-07T10:00:00"));
    }

    @Test
    void createBehaviorReturns404WhenShopDoesNotExist() throws Exception {
        mockMvc.perform(post("/api/user-behaviors")
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("""
                                {
                                  "userId": "user-001",
                                  "shopId": 999,
                                  "behaviorType": "favorite",
                                  "source": "detail_page"
                                }
                                """))
                .andExpect(status().isNotFound())
                .andExpect(jsonPath("$.success").value(false))
                .andExpect(jsonPath("$.message").value("商户不存在"));
    }

    @Test
    @WithMockUser(username = "user-002")
    void listUserBehaviorsReturnsRecentBehaviors() throws Exception {
        mockMvc.perform(post("/api/user-behaviors")
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("""
                                {
                                  "userId": "user-002",
                                  "shopId": 3,
                                  "behaviorType": "view",
                                  "source": "detail_page",
                                  "occurredAt": "2026-07-07T09:00:00"
                                }
                                """))
                .andExpect(status().isCreated());

        mockMvc.perform(post("/api/user-behaviors")
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("""
                                {
                                  "userId": "user-002",
                                  "shopId": 7,
                                  "behaviorType": "rating",
                                  "score": 5.0,
                                  "source": "detail_page",
                                  "occurredAt": "2026-07-07T11:00:00"
                                }
                                """))
                .andExpect(status().isCreated());

        mockMvc.perform(get("/api/user-behaviors")
                        .param("userId", "user-002")
                        .param("limit", "10"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.success").value(true))
                .andExpect(jsonPath("$.data", hasSize(2)))
                .andExpect(jsonPath("$.data[0].shopId").value(7))
                .andExpect(jsonPath("$.data[0].behaviorType").value("rating"))
                .andExpect(jsonPath("$.data[0].score").value(5.0))
                .andExpect(jsonPath("$.data[1].shopId").value(3));
    }

    @Test
    void createBehaviorReturns400WhenUserIdIsBlank() throws Exception {
        mockMvc.perform(post("/api/user-behaviors")
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("""
                                {
                                  "userId": "   ",
                                  "shopId": 3,
                                  "behaviorType": "view",
                                  "source": "detail_page"
                                }
                                """))
                .andExpect(status().isBadRequest())
                .andExpect(jsonPath("$.success").value(false));
    }
}
