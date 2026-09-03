package com.example.locallife.memory;

public record CatalogPreferenceValidationResponse(
        boolean valid,
        String displayLabel
) {}
