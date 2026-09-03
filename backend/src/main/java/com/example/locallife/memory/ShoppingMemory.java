package com.example.locallife.memory;

import java.time.LocalDateTime;

record ShoppingMemory(String id, String ownerUserId, String category, String semanticKey,
                      String tokenValue, int version, String status, LocalDateTime expiresAt,
                      String supersedesId, String commandDigest, String contentDigest) {}
