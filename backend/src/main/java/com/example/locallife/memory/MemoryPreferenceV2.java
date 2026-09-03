package com.example.locallife.memory;

import com.fasterxml.jackson.annotation.JsonIgnoreProperties;
import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.Pattern;

/** Client input is deliberately narrow; the server owns all other record fields. */
@JsonIgnoreProperties(ignoreUnknown = false)
public record MemoryPreferenceV2(
        @NotBlank @Pattern(regexp = "phone|laptop|headphones") String productCategory,
        @NotBlank @Pattern(regexp = "self") String recipientScope,
        @NotBlank @Pattern(regexp = "[a-z_]{1,64}") String semanticKey,
        @NotBlank @Pattern(regexp = "[a-z0-9-]{1,64}") String value,
        // V12 has no server-issued suggestion ledger yet.  Treating an
        // arbitrary client field as a confirmed suggestion would be unsafe.
        @NotBlank @Pattern(regexp = "explicit_user") String source
) {}
