package com.example.locallife.memory;

import com.example.locallife.common.ApiResponse;
import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.databind.DeserializationFeature;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.security.core.Authentication;
import org.springframework.web.bind.annotation.*;
import org.springframework.web.server.ResponseStatusException;
import jakarta.validation.Validator;
import jakarta.validation.Valid;
import java.util.List;
import java.util.Set;

@RestController
@RequestMapping("/api/memory")
@ConditionalOnProperty(prefix="local-life.memory", name="enabled", havingValue="true")
public class ShoppingMemoryController {
    private final ShoppingMemoryService service;
    private final ShoppingMemoryV3Service v3Service;
    private final ObjectMapper objectMapper;
    private final Validator validator;
    public ShoppingMemoryController(ShoppingMemoryService service, ShoppingMemoryV3Service v3Service, ObjectMapper objectMapper, Validator validator) { this.service=service; this.v3Service=v3Service; this.objectMapper=objectMapper; this.validator=validator; }
    @PostMapping("/commands")
    public ResponseEntity<ApiResponse<ShoppingMemoryService.MemoryResponse>> write(@Valid @RequestBody JsonNode raw, Authentication authentication) {
        rejectUnknown(raw, Set.of("commandId", "operation", "predecessorId", "previousVersion", "preference", "consent"));
        rejectUnknown(raw.path("preference"), Set.of("category", "semanticKey", "value"));
        rejectUnknown(raw.path("consent"), Set.of("action", "eventId", "commandDigest", "contentDigest"));
        MemoryWriteRequest request;
        try {
            request = objectMapper.copy().enable(DeserializationFeature.FAIL_ON_UNKNOWN_PROPERTIES).treeToValue(raw, MemoryWriteRequest.class);
        } catch (JsonProcessingException invalid) {
            throw new ResponseStatusException(HttpStatus.BAD_REQUEST, "请求体格式错误");
        }
        if (!validator.validate(request).isEmpty()) throw new ResponseStatusException(HttpStatus.BAD_REQUEST, "请求体格式错误");
        // The legacy self-supplied digest proves payload consistency only. It
        // is not server-issued evidence that the user explicitly consented.
        // Keep the endpoint shape for compatibility, but fail closed until a
        // trusted consent issuer is implemented in a later batch.
        throw new ResponseStatusException(
                HttpStatus.SERVICE_UNAVAILABLE,
                "memory write authorization unavailable"
        );
    }

    @PostMapping("/consents/v2")
    public ApiResponse<MemoryConsentGrant> issueV2Consent(
            @Valid @RequestBody JsonNode raw,
            Authentication authentication
    ) {
        MemoryConsentIssueRequest request = parseV2(
                raw,
                Set.of("operation", "predecessorId", "previousVersion", "preference", "consentAction"),
                MemoryConsentIssueRequest.class
        );
        return ApiResponse.ok(service.issueV2Consent(authentication.getName(), request));
    }

    @PostMapping("/commands/v2")
    public ApiResponse<ShoppingMemoryService.V2MemoryResponse> writeV2(
            @Valid @RequestBody JsonNode raw,
            Authentication authentication
    ) {
        MemoryCommandV2Request request = parseV2(
                raw,
                Set.of("commandId", "operation", "predecessorId", "previousVersion", "preference", "consentEventId"),
                MemoryCommandV2Request.class
        );
        return ApiResponse.ok(service.writeV2(authentication.getName(), request));
    }

    @PostMapping("/consents/v3")
    public ApiResponse<MemoryConsentGrant> issueV3Consent(
            @Valid @RequestBody JsonNode raw,
            Authentication authentication
    ) {
        MemoryConsentIssueV3Request request = parseV3(
                raw,
                Set.of("operation", "predecessorId", "previousVersion", "preference", "consentAction"),
                MemoryConsentIssueV3Request.class
        );
        return ApiResponse.ok(v3Service.issueConsent(authentication.getName(), request));
    }

    @PostMapping("/catalog/validate/v3")
    public ApiResponse<CatalogPreferenceValidationResponse> validateV3Candidate(
            @Valid @RequestBody JsonNode raw,
            Authentication authentication
    ) {
        MemoryPreferenceV3 preference = parseV3Preference(raw);
        return ApiResponse.ok(
                v3Service.validateCandidate(authentication.getName(), preference)
        );
    }

    @PostMapping("/commands/v3")
    public ApiResponse<ShoppingMemoryV3Service.V3MemoryResponse> writeV3(
            @Valid @RequestBody JsonNode raw,
            Authentication authentication
    ) {
        MemoryCommandV3Request request = parseV3(
                raw,
                Set.of("commandId", "operation", "predecessorId", "previousVersion", "preference", "consentEventId"),
                MemoryCommandV3Request.class
        );
        return ApiResponse.ok(v3Service.write(authentication.getName(), request));
    }

    private <T> T parseV2(JsonNode raw, Set<String> rootFields, Class<T> type) {
        rejectUnknown(raw, rootFields);
        rejectUnknown(raw.path("preference"), Set.of(
                "productCategory", "recipientScope", "semanticKey", "value", "source"
        ));
        try {
            T parsed = objectMapper.copy()
                    .enable(DeserializationFeature.FAIL_ON_UNKNOWN_PROPERTIES)
                    .treeToValue(raw, type);
            if (!validator.validate(parsed).isEmpty()) {
                throw new ResponseStatusException(HttpStatus.BAD_REQUEST, "请求体格式错误");
            }
            return parsed;
        } catch (JsonProcessingException invalid) {
            throw new ResponseStatusException(HttpStatus.BAD_REQUEST, "请求体格式错误");
        }
    }

    private <T> T parseV3(JsonNode raw, Set<String> rootFields, Class<T> type) {
        rejectUnknown(raw, rootFields);
        rejectUnknown(raw.path("preference"), Set.of(
                "categoryId", "preferenceKind", "attributeKey",
                "normalizedValue", "catalogRevision", "recipientScope", "source"
        ));
        try {
            T parsed = objectMapper.copy()
                    .enable(DeserializationFeature.FAIL_ON_UNKNOWN_PROPERTIES)
                    .treeToValue(raw, type);
            if (!validator.validate(parsed).isEmpty()) {
                throw new ResponseStatusException(HttpStatus.BAD_REQUEST, "请求体格式错误");
            }
            return parsed;
        } catch (JsonProcessingException invalid) {
            throw new ResponseStatusException(HttpStatus.BAD_REQUEST, "请求体格式错误");
        }
    }

    private MemoryPreferenceV3 parseV3Preference(JsonNode raw) {
        rejectUnknown(raw, Set.of(
                "categoryId", "preferenceKind", "attributeKey",
                "normalizedValue", "catalogRevision", "recipientScope", "source"
        ));
        try {
            MemoryPreferenceV3 parsed = objectMapper.copy()
                    .enable(DeserializationFeature.FAIL_ON_UNKNOWN_PROPERTIES)
                    .treeToValue(raw, MemoryPreferenceV3.class);
            if (!validator.validate(parsed).isEmpty()) {
                throw new ResponseStatusException(HttpStatus.BAD_REQUEST, "请求体格式错误");
            }
            return parsed;
        } catch (JsonProcessingException invalid) {
            throw new ResponseStatusException(HttpStatus.BAD_REQUEST, "请求体格式错误");
        }
    }
    private static void rejectUnknown(JsonNode value, Set<String> allowed) {
        if (!value.isObject()) throw new ResponseStatusException(HttpStatus.BAD_REQUEST, "请求体格式错误");
        java.util.Iterator<String> names = value.fieldNames();
        while (names.hasNext()) if (!allowed.contains(names.next())) throw new ResponseStatusException(HttpStatus.BAD_REQUEST, "请求体格式错误");
    }
    @GetMapping("/projection")
    public ApiResponse<MemoryProjectionResponse> projection(Authentication authentication) { return ApiResponse.ok(service.projectionEnvelope(authentication.getName())); }

    @GetMapping("/projection/v2")
    public ApiResponse<MemoryProjectionV2Response> projectionV2(Authentication authentication) {
        return ApiResponse.ok(service.projectionV2Envelope(authentication.getName()));
    }

    @GetMapping("/projection/v3")
    public ApiResponse<MemoryProjectionV3Response> projectionV3(Authentication authentication) {
        return ApiResponse.ok(v3Service.projection(authentication.getName()));
    }
}
