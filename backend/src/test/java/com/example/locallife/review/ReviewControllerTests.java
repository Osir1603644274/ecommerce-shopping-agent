package com.example.locallife.review;

import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.autoconfigure.web.servlet.AutoConfigureMockMvc;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.http.MediaType;
import org.springframework.security.test.context.support.WithMockUser;
import org.springframework.test.context.bean.override.mockito.MockitoBean;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.transaction.annotation.Propagation;
import org.springframework.transaction.annotation.Transactional;

import static org.hamcrest.Matchers.hasSize;
import static org.hamcrest.Matchers.startsWith;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.Mockito.doThrow;
import static org.mockito.Mockito.verify;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.delete;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.put;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

@SpringBootTest
@AutoConfigureMockMvc
@Transactional
@WithMockUser(roles = "ADMIN")
class ReviewControllerTests {

    private static final int INITIAL_REVIEW_COUNT = 450;
    private static final int REVIEWS_PER_SHOP = 30;

    @Autowired
    private MockMvc mockMvc;

    @MockitoBean
    private ReviewVectorSyncClient reviewVectorSyncClient;

    @Test
    void listReviewsReturnsAllReviews() throws Exception {
        mockMvc.perform(get("/api/reviews"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.success").value(true))
                .andExpect(jsonPath("$.data", hasSize(INITIAL_REVIEW_COUNT)))
                .andExpect(jsonPath("$.data[0].reviewId").value("review-001"))
                .andExpect(jsonPath("$.data[0].shopName").value("巷子口火锅"))
                .andExpect(jsonPath("$.data[0].tags[0]").value("火锅"))
                .andExpect(jsonPath("$.data[0].createdAt").doesNotExist());
    }

    @Test
    void listReviewsCanFilterByShopId() throws Exception {
        mockMvc.perform(get("/api/reviews").param("shopId", "3"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.success").value(true))
                .andExpect(jsonPath("$.data", hasSize(REVIEWS_PER_SHOP)))
                .andExpect(jsonPath("$.data[0].reviewId").value("review-005"))
                .andExpect(jsonPath("$.data[0].shopId").value(3))
                .andExpect(jsonPath("$.data[1].reviewId").value("review-006"))
                .andExpect(jsonPath("$.data[2].reviewId").value("review-013"));
    }

    @Test
    void createReviewReturnsCreatedReview() throws Exception {
        mockMvc.perform(post("/api/reviews")
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("""
                                {
                                  "shopId": 3,
                                  "content": "  新增评论正文  ",
                                  "tags": ["安静", "办公", "安静"]
                                }
                                """))
                .andExpect(status().isCreated())
                .andExpect(jsonPath("$.success").value(true))
                .andExpect(jsonPath("$.data.reviewId", startsWith("review-")))
                .andExpect(jsonPath("$.data.shopId").value(3))
                .andExpect(jsonPath("$.data.shopName").value("清晨手冲咖啡"))
                .andExpect(jsonPath("$.data.content").value("新增评论正文"))
                .andExpect(jsonPath("$.data.tags", hasSize(2)));

        verify(reviewVectorSyncClient).upsert(any(ReviewResponse.class), eq("清晨手冲咖啡"));
    }

    @Test
    void createReviewReturns404WhenShopDoesNotExist() throws Exception {
        mockMvc.perform(post("/api/reviews")
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("""
                                {
                                  "shopId": 999,
                                  "content": "商户不存在时不能保存",
                                  "tags": []
                                }
                                """))
                .andExpect(status().isNotFound())
                .andExpect(jsonPath("$.success").value(false))
                .andExpect(jsonPath("$.message").value("商户不存在"));
    }

    @Test
    void createReviewReturns400WhenContentIsBlank() throws Exception {
        mockMvc.perform(post("/api/reviews")
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("""
                                {
                                  "shopId": 3,
                                  "content": "   ",
                                  "tags": []
                                }
                                """))
                .andExpect(status().isBadRequest())
                .andExpect(jsonPath("$.success").value(false))
                .andExpect(jsonPath("$.message").value("评论正文不能为空"));
    }

    @Test
    void createReviewReturns400WhenJsonIsMalformed() throws Exception {
        mockMvc.perform(post("/api/reviews")
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("{\"shopId\": 3"))
                .andExpect(status().isBadRequest())
                .andExpect(jsonPath("$.success").value(false))
                .andExpect(jsonPath("$.message").value("请求体格式错误"));
    }

    @Test
    void updateReviewReturnsUpdatedReview() throws Exception {
        mockMvc.perform(put("/api/reviews/review-005")
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("""
                                {
                                  "content": "  修改后的评论正文  ",
                                  "tags": ["插座", "办公", "插座"]
                                }
                                """))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.success").value(true))
                .andExpect(jsonPath("$.data.reviewId").value("review-005"))
                .andExpect(jsonPath("$.data.shopId").value(3))
                .andExpect(jsonPath("$.data.content").value("修改后的评论正文"))
                .andExpect(jsonPath("$.data.tags", hasSize(2)));

        verify(reviewVectorSyncClient).upsert(any(ReviewResponse.class), eq("清晨手冲咖啡"));
    }

    @Test
    void updateReviewReturns404WhenReviewDoesNotExist() throws Exception {
        mockMvc.perform(put("/api/reviews/review-missing")
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("""
                                {
                                  "content": "修改不存在的评论",
                                  "tags": []
                                }
                                """))
                .andExpect(status().isNotFound())
                .andExpect(jsonPath("$.success").value(false))
                .andExpect(jsonPath("$.message").value("评论不存在"));
    }

    @Test
    void deleteReviewRemovesReview() throws Exception {
        mockMvc.perform(delete("/api/reviews/review-012"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.success").value(true));

        verify(reviewVectorSyncClient).delete("review-012");

        mockMvc.perform(get("/api/reviews").param("shopId", "6"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.data", hasSize(REVIEWS_PER_SHOP - 1)))
                .andExpect(jsonPath("$.data[0].reviewId").value("review-011"));
    }

    @Test
    void deleteReviewReturns404WhenReviewDoesNotExist() throws Exception {
        mockMvc.perform(delete("/api/reviews/review-missing"))
                .andExpect(status().isNotFound())
                .andExpect(jsonPath("$.success").value(false))
                .andExpect(jsonPath("$.message").value("评论不存在"));
    }

    @Test
    @Transactional(propagation = Propagation.NOT_SUPPORTED)
    void createReviewRollsBackWhenVectorSyncFails() throws Exception {
        doThrow(new ReviewVectorSyncException("评论向量同步失败", new RuntimeException()))
                .when(reviewVectorSyncClient)
                .upsert(any(ReviewResponse.class), anyString());

        mockMvc.perform(post("/api/reviews")
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("""
                                {
                                  "shopId": 3,
                                  "content": "同步失败时不能保存",
                                  "tags": ["回滚"]
                                }
                                """))
                .andExpect(status().isServiceUnavailable())
                .andExpect(jsonPath("$.success").value(false))
                .andExpect(jsonPath("$.message").value("评论向量同步失败，请稍后重试"));

        mockMvc.perform(get("/api/reviews"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.data", hasSize(INITIAL_REVIEW_COUNT)));
    }

    @Test
    @Transactional(propagation = Propagation.NOT_SUPPORTED)
    void deleteReviewRollsBackWhenVectorSyncFails() throws Exception {
        doThrow(new ReviewVectorSyncException("评论向量同步失败", new RuntimeException()))
                .when(reviewVectorSyncClient)
                .delete("review-012");

        mockMvc.perform(delete("/api/reviews/review-012"))
                .andExpect(status().isServiceUnavailable())
                .andExpect(jsonPath("$.success").value(false));

        mockMvc.perform(get("/api/reviews").param("shopId", "6"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.data", hasSize(REVIEWS_PER_SHOP)));
    }
}
