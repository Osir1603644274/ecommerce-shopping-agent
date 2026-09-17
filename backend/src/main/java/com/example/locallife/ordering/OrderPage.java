package com.example.locallife.ordering;

import java.util.List;

public record OrderPage(List<OrderResponse> orders, String nextCursor, boolean hasMore) {
    public OrderPage { orders = List.copyOf(orders); }
}
