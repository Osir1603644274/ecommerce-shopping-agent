package com.example.locallife.memory;

import com.fasterxml.jackson.annotation.JsonIgnoreProperties;
import jakarta.validation.Valid;
import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.NotNull;
import jakarta.validation.constraints.Pattern;

/** The authenticated user explicitly asks the server to mint one write consent. */
@JsonIgnoreProperties(ignoreUnknown = false)
public record MemoryConsentIssueRequest(
        @NotBlank @Pattern(regexp = "write|update|revoke|suppress") String operation,
        String predecessorId,
        Integer previousVersion,
        @NotNull @Valid MemoryPreferenceV2 preference,
        @NotBlank @Pattern(regexp = "remember|confirm_suggestion|forget|disable") String consentAction
) {}
