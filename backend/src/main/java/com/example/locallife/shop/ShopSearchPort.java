package com.example.locallife.shop;

import java.util.List;
import java.util.Optional;

public interface ShopSearchPort {
    Optional<List<Long>> search(Long typeId, String name, int limit);
}
