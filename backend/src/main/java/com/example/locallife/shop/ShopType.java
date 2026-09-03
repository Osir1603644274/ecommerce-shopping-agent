package com.example.locallife.shop;

import java.time.LocalDateTime;

public record ShopType(Long id, String name, Integer sort, LocalDateTime createdAt, LocalDateTime updatedAt) {
}
