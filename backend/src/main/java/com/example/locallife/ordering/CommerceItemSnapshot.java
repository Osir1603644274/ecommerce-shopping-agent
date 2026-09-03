package com.example.locallife.ordering;

public record CommerceItemSnapshot(
        String itemType,
        Long itemId,
        String title,
        long unitPriceMinor,
        String currency,
        long entityVersion,
        String evidenceJson
) {
}
