package com.example.locallife.payment;

import java.util.List;

public record PartialRefundView(String id,String orderId,String status,long amountMinor,String currency,
                                String providerRefundNo,List<Item> items) {
    public record Item(Long itemId,int quantity,long amountMinor) { }
}
