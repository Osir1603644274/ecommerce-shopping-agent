package com.example.locallife.integration;

public final class DomainEventTypes {
    public static final String REVIEW_VECTOR_UPSERT_V1 = "review.vector.upsert.v1";
    public static final String REVIEW_VECTOR_DELETE_V1 = "review.vector.delete.v1";
    public static final String ORDER_CREATED_V1 = "order.created.v1";
    public static final String ORDER_PAID_V1 = "order.paid.v1";
    public static final String ORDER_CANCELLED_V1 = "order.cancelled.v1";
    public static final String ORDER_EXPIRED_V1 = "order.expired.v1";
    public static final String ORDER_REFUNDED_V1 = "order.refunded.v1";
    public static final String ORDER_PARTIAL_REFUNDED_V2 = "order.partial-refunded.v2";
    public static final String SHOP_UPDATED_V1 = "shop.updated.v1";
    public static final String PRODUCT_SEARCH_UPSERT_V1 = "product.search.upsert.v1";
    public static final String PRODUCT_SEARCH_DELETE_V1 = "product.search.delete.v1";

    private DomainEventTypes() {
    }
}
