package com.example.locallife.product;

public record ProductCacheLookup(State state, ProductDetailResponse detail) {
    enum State { HIT, EMPTY, MISS }

    static ProductCacheLookup hit(ProductDetailResponse detail) {
        return new ProductCacheLookup(State.HIT, detail);
    }

    static ProductCacheLookup empty() {
        return new ProductCacheLookup(State.EMPTY, null);
    }

    static ProductCacheLookup miss() {
        return new ProductCacheLookup(State.MISS, null);
    }

    boolean isHit() {
        return state == State.HIT;
    }

    boolean isEmpty() {
        return state == State.EMPTY;
    }
}
