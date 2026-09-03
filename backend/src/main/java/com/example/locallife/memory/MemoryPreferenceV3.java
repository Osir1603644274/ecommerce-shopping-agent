package com.example.locallife.memory;

import com.fasterxml.jackson.annotation.JsonIgnoreProperties;
import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.Pattern;
import jakarta.validation.constraints.Size;

/** Catalog-bound V13 preference. The browser never constructs this value. */
@JsonIgnoreProperties(ignoreUnknown = false)
public record MemoryPreferenceV3(
        @NotBlank @Pattern(regexp = "[a-z0-9][a-z0-9_-]{0,63}") String categoryId,
        @NotBlank @Pattern(regexp = "prefer|avoid|indifferent") String preferenceKind,
        @NotBlank @Pattern(regexp = "[a-z][a-z0-9_]{0,63}") String attributeKey,
        @NotBlank @Size(max = 128)
        @Pattern(regexp = "[a-z0-9][a-z0-9._:-]{0,127}") String normalizedValue,
        @NotBlank @Pattern(regexp = "[A-Za-z0-9._:-]{1,64}") String catalogRevision,
        @NotBlank @Pattern(regexp = "self") String recipientScope,
        @NotBlank @Pattern(regexp = "user_confirmed") String source
) {}
