package com.example.locallife.shop;

public record ShopCacheLookup(Status status, ShopDetailResponse detail) {

    public enum Status {
        HIT,
        EMPTY,
        MISS
    }

    public static ShopCacheLookup hit(ShopDetailResponse detail) {
        return new ShopCacheLookup(Status.HIT, detail);
    }

    public static ShopCacheLookup empty() {
        return new ShopCacheLookup(Status.EMPTY, null);
    }

    public static ShopCacheLookup miss() {
        return new ShopCacheLookup(Status.MISS, null);
    }

    public boolean isHit() {
        return status == Status.HIT;
    }

    public boolean isEmpty() {
        return status == Status.EMPTY;
    }
}
