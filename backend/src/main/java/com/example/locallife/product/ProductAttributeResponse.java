package com.example.locallife.product;

public record ProductAttributeResponse(
        String key,
        String valueType,
        String rawValue,
        String normalizedText,
        Double normalizedNumber,
        Boolean normalizedBoolean,
        String unit,
        String evidenceField,
        String extractionMethod,
        Double confidence
) {
}
