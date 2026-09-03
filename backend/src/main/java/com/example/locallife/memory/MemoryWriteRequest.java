package com.example.locallife.memory;

import com.fasterxml.jackson.annotation.JsonIgnoreProperties;
import jakarta.validation.Valid;
import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.NotNull;
import jakarta.validation.constraints.Pattern;

@JsonIgnoreProperties(ignoreUnknown = false)
public record MemoryWriteRequest(
        @NotBlank @Pattern(regexp = "[A-Za-z0-9_-]{1,64}") String commandId,
        @NotBlank @Pattern(regexp = "write|update|revoke|suppress") String operation,
        String predecessorId,
        Integer previousVersion,
        @NotNull @Valid MemoryPreference preference,
        @NotNull @Valid MemoryConsent consent
) {
    @JsonIgnoreProperties(ignoreUnknown = false)
    public record MemoryPreference(@NotBlank @Pattern(regexp = "[a-z_]{1,32}") String category, @NotBlank @Pattern(regexp = "[a-z_]{1,64}") String semanticKey, @NotBlank @Pattern(regexp = "[a-z0-9-]{1,64}") String value) {}
    @JsonIgnoreProperties(ignoreUnknown = false)
    public record MemoryConsent(@NotBlank @Pattern(regexp = "grant|withdraw") String action, @NotBlank @Pattern(regexp = "[A-Za-z0-9_-]{1,64}") String eventId, @NotBlank @Pattern(regexp = "[0-9a-f]{64}") String commandDigest, @NotBlank @Pattern(regexp = "[0-9a-f]{64}") String contentDigest) {}
}
