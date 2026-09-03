package com.example.locallife.ordering;

import com.example.locallife.review.ReviewVectorSyncClient;
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
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

@SpringBootTest
@AutoConfigureMockMvc
@Transactional
class CommerceHttpContractTests {
    @Autowired
    private MockMvc mockMvc;

    @Autowired
    private ObjectMapper objectMapper;

    @Autowired
    private com.example.locallife.inventory.InventoryService inventoryService;

    @MockitoBean
    private ReviewVectorSyncClient reviewVectorSyncClient;

    @Test
    void authenticatedCheckoutPaymentAndOrderReadKeepApiEnvelope() throws Exception {
        inventoryService.createStock("PRODUCT", 1001L, 2);
        String accessToken = register();

        mockMvc.perform(post("/api/orders/preview")
                        .header("Authorization", "Bearer " + accessToken)
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("""
                                {
                                  "itemType":"PRODUCT",
                                  "itemId":1001,
                                  "quantity":1
                                }
                                """))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.success").value(true))
                .andExpect(jsonPath("$.data.title")
                        .value("远航 P1 5G 手机 12GB+256GB 5000mAh"))
                .andExpect(jsonPath("$.data.payableMinor").value(249900))
                .andExpect(jsonPath("$.data.availableQuantity").value(2));
        org.assertj.core.api.Assertions.assertThat(
                inventoryService.getStock("PRODUCT", 1001L).availableQuantity()
        ).isEqualTo(2);

        mockMvc.perform(post("/api/orders/preview")
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("""
                                {
                                  "itemType":"PRODUCT",
                                  "itemId":1001,
                                  "quantity":1
                                }
                                """))
                .andExpect(status().isUnauthorized())
                .andExpect(jsonPath("$.success").value(false));

        String createBody = mockMvc.perform(post("/api/orders")
                        .header("Authorization", "Bearer " + accessToken)
                        .header("Idempotency-Key", "http-checkout-1")
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("""
                                {
                                  "itemType":"PRODUCT",
                                  "itemId":1001,
                                  "quantity":1
                                }
                                """))
                .andExpect(status().isCreated())
                .andExpect(jsonPath("$.success").value(true))
                .andExpect(jsonPath("$.data.status").value("PENDING_PAYMENT"))
                .andExpect(jsonPath("$.data.payableMinor").value(249900))
                .andReturn().getResponse().getContentAsString();
        String orderId = objectMapper.readTree(createBody).path("data").path("id").asText();
        String orderNo = objectMapper.readTree(createBody).path("data").path("orderNo").asText();

        mockMvc.perform(get("/api/orders/by-idempotency-key/{key}", "http-checkout-1")
                        .header("Authorization", "Bearer " + accessToken))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.data.id").value(orderId))
                .andExpect(jsonPath("$.data.orderNo").value(orderNo));

        String paymentBody = mockMvc.perform(post("/api/payments/orders/{orderId}", orderId)
                        .header("Authorization", "Bearer " + accessToken))
                .andExpect(status().isCreated())
                .andExpect(jsonPath("$.success").value(true))
                .andExpect(jsonPath("$.data.status").value("CREATED"))
                .andReturn().getResponse().getContentAsString();
        String paymentId = objectMapper.readTree(paymentBody).path("data").path("id").asText();

        mockMvc.perform(get("/api/payments/orders/{orderId}", orderId)
                        .header("Authorization", "Bearer " + accessToken))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.data.id").value(paymentId))
                .andExpect(jsonPath("$.data.orderId").value(orderId));

        mockMvc.perform(post("/api/payments/{paymentId}/simulate-success", paymentId)
                        .header("Authorization", "Bearer " + accessToken))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.data.status").value("SUCCESS"));

        mockMvc.perform(get("/api/orders/{orderId}", orderId)
                        .header("Authorization", "Bearer " + accessToken))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.data.id").value(orderId))
                .andExpect(jsonPath("$.data.status").value("PAID"));

        mockMvc.perform(get("/api/orders/{orderReference}", orderNo)
                        .header("Authorization", "Bearer " + accessToken))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.data.id").value(orderId))
                .andExpect(jsonPath("$.data.orderNo").value(orderNo))
                .andExpect(jsonPath("$.data.status").value("PAID"));

        String otherUserToken = register();
        mockMvc.perform(get("/api/orders/by-idempotency-key/{key}", "http-checkout-1")
                        .header("Authorization", "Bearer " + otherUserToken))
                .andExpect(status().isNotFound())
                .andExpect(jsonPath("$.success").value(false));
        mockMvc.perform(get("/api/orders/{orderReference}", orderNo)
                        .header("Authorization", "Bearer " + otherUserToken))
                .andExpect(status().isForbidden())
                .andExpect(jsonPath("$.success").value(false));

        mockMvc.perform(get("/api/orders/{orderReference}", UUID.randomUUID())
                        .header("Authorization", "Bearer " + accessToken))
                .andExpect(status().isNotFound())
                .andExpect(jsonPath("$.success").value(false));

        mockMvc.perform(get("/api/orders/{orderReference}", orderNo))
                .andExpect(status().isUnauthorized())
                .andExpect(jsonPath("$.success").value(false));

        mockMvc.perform(get("/api/orders"))
                .andExpect(status().isUnauthorized())
                .andExpect(jsonPath("$.success").value(false));
    }

    @Test
    void publicPaymentCallbackStillRequiresValidSignature() throws Exception {
        mockMvc.perform(post("/api/payments/callbacks/LOCAL_SIMULATOR")
                        .header("X-Payment-Signature", "invalid")
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("""
                                {
                                  "eventId":"event-invalid",
                                  "paymentNo":"unknown",
                                  "providerTradeNo":"unknown",
                                  "amountMinor":1,
                                  "status":"SUCCESS",
                                  "timestamp":0
                                }
                                """))
                .andExpect(status().isForbidden())
                .andExpect(jsonPath("$.success").value(false));
    }

    private String register() throws Exception {
        String username = "commerce-" + UUID.randomUUID();
        String body = mockMvc.perform(post("/api/auth/register")
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("""
                                {"username":"%s","password":"StrongPassword123!"}
                                """.formatted(username)))
                .andExpect(status().isCreated())
                .andReturn().getResponse().getContentAsString();
        JsonNode json = objectMapper.readTree(body);
        return json.path("data").path("accessToken").asText();
    }
}
