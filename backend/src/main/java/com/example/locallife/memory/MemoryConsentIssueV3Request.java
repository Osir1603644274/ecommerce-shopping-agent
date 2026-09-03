package com.example.locallife.memory;

import com.fasterxml.jackson.annotation.JsonIgnoreProperties;
import jakarta.validation.Valid;
import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.NotNull;
import jakarta.validation.constraints.Pattern;

@JsonIgnoreProperties(ignoreUnknown = false)
public record MemoryConsentIssueV3Request(
        @NotBlank @Pattern(regexp = "write|update|revoke|suppress") String operation,
        String predecessorId,
        Integer previousVersion,
        @NotNull @Valid MemoryPreferenceV3 preference,
        @NotBlank @Pattern(regexp = "confirm_suggestion|forget|disable") String consentAction
) {}
