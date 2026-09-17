package com.example.locallife.inventory;

/** Shipment gate, not a command journal mutation API. */
public interface InventorySettlement {
    boolean settled(String orderId);
}
