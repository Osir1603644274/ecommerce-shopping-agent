package com.example.locallife.review;

import java.util.List;

public record ReviewVectorSyncRequest(
        Long shopId,
        String shopName,
        String content,
        String source,
        String language,
        String contentZh,
        String translationStatus,
        List<String> tags
) {
}
