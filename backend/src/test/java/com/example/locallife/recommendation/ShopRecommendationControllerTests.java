package com.example.locallife.recommendation;

import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.autoconfigure.web.servlet.AutoConfigureMockMvc;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.http.MediaType;
import org.springframework.security.test.context.support.WithMockUser;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.transaction.annotation.Transactional;

import static org.hamcrest.Matchers.greaterThan;
import static org.hamcrest.Matchers.hasSize;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

@SpringBootTest
@AutoConfigureMockMvc
@Transactional
@WithMockUser(roles = "SERVICE")
class ShopRecommendationControllerTests {

    @Autowired
    private MockMvc mockMvc;

    @Test
    void recommendShopsRanksItemcfCandidatesBeforeOtherCandidates() throws Exception {
        createBehavior("user-rec-001", 3, "favorite", null, "2026-07-07T10:00:00");
        createBehavior("similar-user-001", 3, "favorite", null, "2026-07-06T10:00:00");
        createBehavior("similar-user-001", 7, "favorite", null, "2026-07-06T10:01:00");
        createBehavior("similar-user-002", 3, "favorite", null, "2026-07-06T11:00:00");
        createBehavior("similar-user-002", 7, "favorite", null, "2026-07-06T11:01:00");
        createBehavior("similar-user-003", 3, "favorite", null, "2026-07-06T12:00:00");
        createBehavior("similar-user-003", 8, "favorite", null, "2026-07-06T12:01:00");

        mockMvc.perform(get("/api/recommendations/shops")
                        .param("userId", "user-rec-001")
                        .param("limit", "10"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.success").value(true))
                .andExpect(jsonPath("$.data", hasSize(2)))
                .andExpect(jsonPath("$.data[0].shopId").value(7))
                .andExpect(jsonPath("$.data[0].typeId").value(2))
                .andExpect(jsonPath("$.data[0].reason").value("\u548c\u4f60\u5173\u6ce8\u8fc7\u7684\u5546\u6237\u76f8\u4f3c\uff0c\u540c\u65f6\u8fd1\u671f\u4e5f\u8f83\u70ed\u95e8"))
                .andExpect(jsonPath("$.data[0].triggerShopIds[0]").value(3))
                .andExpect(jsonPath("$.data[0].triggerShopNames[0]").value("清晨手冲咖啡"))
                .andExpect(jsonPath("$.data[0].score", greaterThan(0.0)))
                .andExpect(jsonPath("$.data[0].distanceMeters").doesNotExist())
                .andExpect(jsonPath("$.data[1].shopId").value(8));
    }

    @Test
    void recommendShopsReturnsPopularFallbackWhenUserHasNoBehavior() throws Exception {
        createBehavior("popular-user-001", 1, "favorite", null, "2026-07-06T10:00:00");
        createBehavior("popular-user-002", 1, "favorite", null, "2026-07-06T11:00:00");
        createBehavior("popular-user-003", 2, "favorite", null, "2026-07-06T12:00:00");

        mockMvc.perform(get("/api/recommendations/shops")
                        .param("userId", "new-user")
                        .param("limit", "10"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.success").value(true))
                .andExpect(jsonPath("$.data", hasSize(2)))
                .andExpect(jsonPath("$.data[0].shopId").value(1))
                .andExpect(jsonPath("$.data[0].reason").value("\u8fd1\u671f\u70ed\u95e8\u5546\u6237"))
                .andExpect(jsonPath("$.data[1].shopId").value(2));
    }

    @Test
    void recommendShopsCanCalculateDistanceWhenLocationProvided() throws Exception {
        createBehavior("user-rec-002", 3, "favorite", null, "2026-07-07T10:00:00");
        createBehavior("similar-user-004", 3, "favorite", null, "2026-07-06T10:00:00");
        createBehavior("similar-user-004", 7, "favorite", null, "2026-07-06T10:01:00");
        createBehavior("similar-user-005", 3, "favorite", null, "2026-07-06T11:00:00");
        createBehavior("similar-user-005", 8, "favorite", null, "2026-07-06T11:01:00");

        mockMvc.perform(get("/api/recommendations/shops")
                        .param("userId", "user-rec-002")
                        .param("longitude", "116.4000")
                        .param("latitude", "39.9000")
                        .param("radiusMeters", "2000")
                        .param("limit", "10"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.success").value(true))
                .andExpect(jsonPath("$.data", hasSize(2)))
                .andExpect(jsonPath("$.data[0].shopId").value(7))
                .andExpect(jsonPath("$.data[0].distanceMeters", greaterThan(0.0)))
                .andExpect(jsonPath("$.data[1].shopId").value(8))
                .andExpect(jsonPath("$.data[1].distanceMeters", greaterThan(0.0)));
    }

    @Test
    void recommendShopsUsesTypeFallbackWhenHybridCandidatePoolIsInsufficient() throws Exception {
        createBehavior("user-rec-type-fallback", 3, "favorite", null, "2026-07-07T10:00:00");

        mockMvc.perform(get("/api/recommendations/shops")
                        .param("userId", "user-rec-type-fallback")
                        .param("limit", "10"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.success").value(true))
                .andExpect(jsonPath("$.data", hasSize(2)))
                .andExpect(jsonPath("$.data[0].shopId").value(7))
                .andExpect(jsonPath("$.data[0].reason").value("\u4f60\u6700\u8fd1\u5173\u6ce8\u8fc7\u540c\u7c7b\u578b\u5546\u6237"))
                .andExpect(jsonPath("$.data[0].triggerShopIds[0]").value(3))
                .andExpect(jsonPath("$.data[1].shopId").value(8));
    }

    @Test
    void recommendShopsReturns400WhenUserIdIsBlank() throws Exception {
        mockMvc.perform(get("/api/recommendations/shops")
                        .param("userId", "   "))
                .andExpect(status().isBadRequest())
                .andExpect(jsonPath("$.success").value(false));
    }

    private void createBehavior(
            String userId,
            long shopId,
            String behaviorType,
            Double score,
            String occurredAt
    ) throws Exception {
        String scoreField = score == null ? "null" : score.toString();
        mockMvc.perform(post("/api/user-behaviors")
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("""
                                {
                                  "userId": "%s",
                                  "shopId": %d,
                                  "behaviorType": "%s",
                                  "score": %s,
                                  "source": "detail_page",
                                  "occurredAt": "%s"
                                }
                                """.formatted(userId, shopId, behaviorType, scoreField, occurredAt)))
                .andExpect(status().isCreated());
    }
}
