package com.example.locallife.inventory;

/** Public read-only contract; callers cannot access inventory command transport. */
public interface InventoryReadPort {
    java.util.List<InventoryStock> stocks(String itemType,java.util.List<Long> itemIds);
}
