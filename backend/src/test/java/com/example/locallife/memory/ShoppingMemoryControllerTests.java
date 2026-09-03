package com.example.locallife.memory;

import com.example.locallife.review.ReviewVectorSyncClient;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.autoconfigure.web.servlet.AutoConfigureMockMvc;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.http.MediaType;
import org.springframework.test.context.bean.override.mockito.MockitoBean;
import org.springframework.test.web.servlet.MockMvc;

import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.verifyNoInteractions;
import static org.springframework.security.test.web.servlet.request.SecurityMockMvcRequestPostProcessors.user;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

@SpringBootTest(properties = {"local-life.memory.enabled=true", "local-life.messaging.enabled=false"})
@AutoConfigureMockMvc
class ShoppingMemoryControllerTests {
    @Autowired MockMvc mockMvc;
    @MockitoBean ShoppingMemoryService service;
    @MockitoBean ShoppingMemoryV3Service v3Service;
    @MockitoBean ReviewVectorSyncClient reviewVectorSyncClient;

    @Test void memoryWriteRequiresAuthenticationAndRejectsBodyOwnerOrUnknownField() throws Exception {
        String base = """
                {"commandId":"cmd-1","operation":"write","preference":{"category":"shopping_preference","semanticKey":"avoid_brand","value":"brand-x"},"consent":{"action":"grant","eventId":"event-1","commandDigest":"%s","contentDigest":"%s"}}
                """.formatted("a".repeat(64), "b".repeat(64));
        mockMvc.perform(post("/api/memory/commands").contentType(MediaType.APPLICATION_JSON).content(base)).andExpect(status().isUnauthorized());
        mockMvc.perform(post("/api/memory/commands").with(user("jwt-subject")).contentType(MediaType.APPLICATION_JSON).content(base.replace("{", "{\"ownerUserId\":\"other\",")))
                .andExpect(status().isBadRequest());
        mockMvc.perform(post("/api/memory/commands").with(user("jwt-subject")).contentType(MediaType.APPLICATION_JSON).content(base.replace("{", "{\"unknown\":true,")))
                .andExpect(status().isBadRequest());
        mockMvc.perform(post("/api/memory/commands").with(user("jwt-subject")).contentType(MediaType.APPLICATION_JSON).content(base.replace("\"commandId\":\"cmd-1\",", "")))
                .andExpect(status().isBadRequest());
        mockMvc.perform(post("/api/memory/commands").with(user("jwt-subject")).contentType(MediaType.APPLICATION_JSON).content(base.replace("\"value\":\"brand-x\"", "")))
                .andExpect(status().isBadRequest());
        verifyNoInteractions(service);
        mockMvc.perform(post("/api/memory/commands").with(user("jwt-subject")).contentType(MediaType.APPLICATION_JSON).content(base)).andExpect(status().isServiceUnavailable());
        verifyNoInteractions(service);
    }

    @Test void projectionUsesAuthenticationOwnerAndExactEnvelope() throws Exception {
        org.mockito.Mockito.when(service.projectionEnvelope("jwt-subject")).thenReturn(new MemoryProjectionResponse(1,7,java.util.List.of()));
        mockMvc.perform(org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get("/api/memory/projection").with(user("jwt-subject")))
                .andExpect(status().isOk())
                .andExpect(org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath("$.data.schemaVersion").value(1))
                .andExpect(org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath("$.data.revision").value(7))
                .andExpect(org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath("$.data.entries").isArray());
        verify(service).projectionEnvelope("jwt-subject");
    }

    @Test void governedProjectionV2RequiresAuthenticationAndPublishesExactSchema() throws Exception {
        org.mockito.Mockito.when(service.projectionV2Envelope("jwt-subject"))
                .thenReturn(new MemoryProjectionV2Response(2,9,MemoryProjectionV2Response.bindOwner("jwt-subject"),true,java.util.List.of()));
        mockMvc.perform(org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get("/api/memory/projection/v2"))
                .andExpect(status().isUnauthorized());
        mockMvc.perform(org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get("/api/memory/projection/v2").with(user("jwt-subject")))
                .andExpect(status().isOk())
                .andExpect(org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath("$.data.schemaVersion").value(2))
                .andExpect(org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath("$.data.revision").value(9))
                .andExpect(org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath("$.data.ownerBinding").value(MemoryProjectionV2Response.bindOwner("jwt-subject")))
                .andExpect(org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath("$.data.truncated").value(true));
        verify(service).projectionV2Envelope("jwt-subject");
    }

    @Test void v2ConsentAndCommandRequireAuthenticationAndRejectUnknownFields() throws Exception {
        String consent = """
                {"operation":"write","preference":{"productCategory":"phone","recipientScope":"self","semanticKey":"os","value":"android","source":"explicit_user"},"consentAction":"remember"}
                """;
        org.mockito.Mockito.when(service.issueV2Consent(eq("jwt-subject"), any()))
                .thenReturn(new MemoryConsentGrant(
                        "command-1", "event-1", "remember",
                        java.time.Instant.parse("2026-08-29T00:05:00Z")
                ));
        mockMvc.perform(post("/api/memory/consents/v2")
                        .contentType(MediaType.APPLICATION_JSON).content(consent))
                .andExpect(status().isUnauthorized());
        mockMvc.perform(post("/api/memory/consents/v2").with(user("jwt-subject"))
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(consent.replace("{", "{\"ownerUserId\":\"other\",")))
                .andExpect(status().isBadRequest());
        mockMvc.perform(post("/api/memory/consents/v2").with(user("jwt-subject"))
                        .contentType(MediaType.APPLICATION_JSON).content(consent))
                .andExpect(status().isOk())
                .andExpect(org.springframework.test.web.servlet.result.MockMvcResultMatchers
                        .jsonPath("$.data.commandId").value("command-1"));
        verify(service).issueV2Consent(eq("jwt-subject"), any());

        String command = """
                {"commandId":"command-1","operation":"write","preference":{"productCategory":"phone","recipientScope":"self","semanticKey":"os","value":"android","source":"explicit_user"},"consentEventId":"event-1"}
                """;
        org.mockito.Mockito.when(service.writeV2(eq("jwt-subject"), any()))
                .thenReturn(new ShoppingMemoryService.V2MemoryResponse("entry-1", 1, "ACTIVE"));
        mockMvc.perform(post("/api/memory/commands/v2").with(user("jwt-subject"))
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(command.replace("{", "{\"unexpected\":true,")))
                .andExpect(status().isBadRequest());
        mockMvc.perform(post("/api/memory/commands/v2").with(user("jwt-subject"))
                        .contentType(MediaType.APPLICATION_JSON).content(command))
                .andExpect(status().isOk())
                .andExpect(org.springframework.test.web.servlet.result.MockMvcResultMatchers
                        .jsonPath("$.data.entryId").value("entry-1"));
        verify(service).writeV2(eq("jwt-subject"), any());
    }

    @Test void v3AcceptsOnlyServerValidatedCatalogPreferenceShape() throws Exception {
        String consent = """
                {"operation":"write","preference":{"categoryId":"exercise-fitness","preferenceKind":"prefer","attributeKey":"equipment_type","normalizedValue":"indoor-bike","catalogRevision":"shopping-companion-v1","recipientScope":"self","source":"user_confirmed"},"consentAction":"confirm_suggestion"}
                """;
        org.mockito.Mockito.when(v3Service.issueConsent(eq("jwt-subject"), any()))
                .thenReturn(new MemoryConsentGrant(
                        "command-3", "event-3", "confirm_suggestion",
                        java.time.Instant.parse("2026-08-29T00:05:00Z")
                ));

        mockMvc.perform(post("/api/memory/consents/v3")
                        .contentType(MediaType.APPLICATION_JSON).content(consent))
                .andExpect(status().isUnauthorized());
        mockMvc.perform(post("/api/memory/consents/v3").with(user("jwt-subject"))
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(consent.replace("{", "{\"ownerUserId\":\"other\",")))
                .andExpect(status().isBadRequest());
        mockMvc.perform(post("/api/memory/consents/v3").with(user("jwt-subject"))
                        .contentType(MediaType.APPLICATION_JSON).content(consent))
                .andExpect(status().isOk())
                .andExpect(org.springframework.test.web.servlet.result.MockMvcResultMatchers
                        .jsonPath("$.data.commandId").value("command-3"));
        verify(v3Service).issueConsent(eq("jwt-subject"), any());
    }
}
