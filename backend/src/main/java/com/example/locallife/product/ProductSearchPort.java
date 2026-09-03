package com.example.locallife.product;

import java.util.List;
import java.util.Optional;

public interface ProductSearchPort {
    Optional<List<Long>> search(
            String query,
            String category,
            String brand,
            Long minPriceMinor,
            Long maxPriceMinor,
            int limit
    );
}
