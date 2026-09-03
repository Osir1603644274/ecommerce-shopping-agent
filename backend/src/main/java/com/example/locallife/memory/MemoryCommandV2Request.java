package com.example.locallife.memory;

import com.fasterxml.jackson.annotation.JsonIgnoreProperties;
import jakarta.validation.Valid;
import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.NotNull;
import jakarta.validation.constraints.Pattern;

/** The opaque consentEventId is server-issued by the preceding explicit action. */
@JsonIgnoreProperties(ignoreUnknown = false)
public record MemoryCommandV2Request(
        @NotBlank @Pattern(regexp = "[A-Za-z0-9_-]{1,64}") String commandId,
        @NotBlank @Pattern(regexp = "write|update|revoke|suppress") String operation,
        String predecessorId,
        Integer previousVersion,
        @NotNull @Valid MemoryPreferenceV2 preference,
        @NotBlank @Pattern(regexp = "[A-Za-z0-9_-]{1,64}") String consentEventId
) {}
