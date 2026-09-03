package com.example.locallife.identity;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.autoconfigure.web.servlet.AutoConfigureMockMvc;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.http.MediaType;
import org.springframework.test.context.bean.override.mockito.MockitoBean;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.transaction.annotation.Transactional;

import java.util.UUID;

import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.put;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

@SpringBootTest
@AutoConfigureMockMvc
@Transactional
class AuthControllerTests {

    @Autowired
    private MockMvc mockMvc;

    @Autowired
    private ObjectMapper objectMapper;

    @MockitoBean
    private com.example.locallife.review.ReviewVectorSyncClient reviewVectorSyncClient;

    @Test
    void registerIssuesAccessAndRefreshTokens() throws Exception {
        String username = uniqueUsername();

        JsonNode tokens = register(username);

        org.assertj.core.api.Assertions.assertThat(tokens.path("accessToken").asText()).isNotBlank();
        org.assertj.core.api.Assertions.assertThat(tokens.path("refreshToken").asText()).isNotBlank();
        org.assertj.core.api.Assertions.assertThat(tokens.path("tokenType").asText()).isEqualTo("Bearer");
        org.assertj.core.api.Assertions.assertThat(tokens.path("accessExpiresInSeconds").asLong())
                .isEqualTo(900);
        org.assertj.core.api.Assertions.assertThat(tokens.path("user").path("username").asText())
                .isEqualTo(username);
        org.assertj.core.api.Assertions.assertThat(tokens.path("user").path("roles").get(0).asText())
                .isEqualTo("USER");
    }

    @Test
    void duplicateUsernameReturnsConflict() throws Exception {
        String username = uniqueUsername();
        register(username);

        mockMvc.perform(post("/api/auth/register")
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(credentials(username)))
                .andExpect(status().isConflict())
                .andExpect(jsonPath("$.success").value(false));
    }

    @Test
    void loginRejectsWrongPassword() throws Exception {
        String username = uniqueUsername();
        register(username);

        mockMvc.perform(post("/api/auth/login")
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("""
                                {"username":"%s","password":"wrong-password"}
                                """.formatted(username)))
                .andExpect(status().isUnauthorized())
                .andExpect(jsonPath("$.success").value(false));
    }

    @Test
    void refreshRotatesTokenAndReuseRevokesFamily() throws Exception {
        JsonNode initial = register(uniqueUsername());
        String firstRefresh = initial.path("refreshToken").asText();

        JsonNode rotated = refresh(firstRefresh, 200);
        String secondRefresh = rotated.path("refreshToken").asText();
        org.assertj.core.api.Assertions.assertThat(secondRefresh).isNotEqualTo(firstRefresh);

        refresh(firstRefresh, 401);
        refresh(secondRefresh, 401);
    }

    @Test
    void logoutRevokesRefreshFamily() throws Exception {
        JsonNode initial = register(uniqueUsername());
        String refreshToken = initial.path("refreshToken").asText();

        mockMvc.perform(post("/api/auth/logout")
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(refreshBody(refreshToken)))
                .andExpect(status().isOk());

        refresh(refreshToken, 401);
    }

    @Test
    void publicReadsRemainAnonymousAndWritesRequireAuthentication() throws Exception {
        mockMvc.perform(get("/api/products"))
                .andExpect(status().isOk());

        mockMvc.perform(post("/api/user-behaviors")
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("""
                                {
                                  "userId":"ignored-anonymous",
                                  "shopId":3,
                                  "behaviorType":"favorite",
                                  "score":null,
                                  "source":"api"
                                }
                                """))
                .andExpect(status().isUnauthorized())
                .andExpect(jsonPath("$.success").value(false));
    }

    @Test
    void regularUserCannotModifyShop() throws Exception {
        String accessToken = register(uniqueUsername()).path("accessToken").asText();

        mockMvc.perform(put("/api/shops/3")
                        .header("Authorization", "Bearer " + accessToken)
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("""
                                {"address":"新地址","avgPrice":45,"phone":"010-12345678"}
                                """))
                .andExpect(status().isForbidden())
                .andExpect(jsonPath("$.success").value(false));
    }

    @Test
    void behaviorWriteUsesAuthenticatedSubjectInsteadOfRequestUserId() throws Exception {
        JsonNode auth = register(uniqueUsername());
        String accessToken = auth.path("accessToken").asText();
        String authenticatedUserId = auth.path("user").path("id").asText();

        mockMvc.perform(post("/api/user-behaviors")
                        .header("Authorization", "Bearer " + accessToken)
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("""
                                {
                                  "userId":"attempted-impersonation",
                                  "shopId":3,
                                  "behaviorType":"favorite",
                                  "score":null,
                                  "source":"api"
                                }
                                """))
                .andExpect(status().isCreated())
                .andExpect(jsonPath("$.data.userId").value(authenticatedUserId));
    }

    @Test
    void reviewOwnerCanUpdateButAnotherUserCannot() throws Exception {
        String ownerAccessToken = register(uniqueUsername()).path("accessToken").asText();
        String otherAccessToken = register(uniqueUsername()).path("accessToken").asText();

        String createResponse = mockMvc.perform(post("/api/reviews")
                        .header("Authorization", "Bearer " + ownerAccessToken)
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("""
                                {
                                  "shopId":3,
                                  "content":"适合学习和办公，环境比较安静。",
                                  "tags":["安静","办公"]
                                }
                                """))
                .andExpect(status().isCreated())
                .andReturn()
                .getResponse()
                .getContentAsString();
        String reviewId = objectMapper.readTree(createResponse).path("data").path("reviewId").asText();

        String updateBody = """
                {"content":"更新后的评价内容足够长。","tags":["安静"]}
                """;
        mockMvc.perform(put("/api/reviews/{reviewId}", reviewId)
                        .header("Authorization", "Bearer " + otherAccessToken)
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(updateBody))
                .andExpect(status().isForbidden());

        mockMvc.perform(put("/api/reviews/{reviewId}", reviewId)
                        .header("Authorization", "Bearer " + ownerAccessToken)
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(updateBody))
                .andExpect(status().isOk());
    }

    private JsonNode register(String username) throws Exception {
        String response = mockMvc.perform(post("/api/auth/register")
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(credentials(username)))
                .andExpect(status().isCreated())
                .andExpect(jsonPath("$.success").value(true))
                .andReturn()
                .getResponse()
                .getContentAsString();
        return objectMapper.readTree(response).path("data");
    }

    private JsonNode refresh(String refreshToken, int expectedStatus) throws Exception {
        String response = mockMvc.perform(post("/api/auth/refresh")
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(refreshBody(refreshToken)))
                .andExpect(status().is(expectedStatus))
                .andReturn()
                .getResponse()
                .getContentAsString();
        return objectMapper.readTree(response).path("data");
    }

    private static String credentials(String username) {
        return """
                {"username":"%s","password":"correct-horse-battery-staple"}
                """.formatted(username);
    }

    private static String refreshBody(String token) {
        return """
                {"refreshToken":"%s"}
                """.formatted(token);
    }

    private static String uniqueUsername() {
        return "user-" + UUID.randomUUID().toString().substring(0, 8);
    }
}
