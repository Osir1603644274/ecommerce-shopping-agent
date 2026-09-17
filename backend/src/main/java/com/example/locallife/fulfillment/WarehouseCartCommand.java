package com.example.locallife.fulfillment;

import java.util.List;

/** V2 command bytes and identity are immutable for the lifetime of each revision. */
public record WarehouseCartCommand(String schemaVersion, String requestKey, String orderId,
                                   long revision, List<Item> items) {
    public record Item(String itemType, Long itemId, int quantity) { }
}
