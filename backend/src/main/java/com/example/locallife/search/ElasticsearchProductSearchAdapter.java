package com.example.locallife.search;

import com.example.locallife.product.ProductSearchPort;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.stereotype.Component;

import java.util.List;
import java.util.Optional;

@Component
@ConditionalOnProperty(prefix = "local-life.search", name = "enabled", havingValue = "true")
class ElasticsearchProductSearchAdapter implements ProductSearchPort {
    private final ElasticsearchGateway gateway;

    ElasticsearchProductSearchAdapter(ElasticsearchGateway gateway) {
        this.gateway = gateway;
    }

    @Override
    public Optional<List<Long>> search(
            String query,
            String category,
            String brand,
            Long minPriceMinor,
            Long maxPriceMinor,
            int limit
    ) {
        return gateway.searchProducts(
                query, category, brand, minPriceMinor, maxPriceMinor, limit);
    }
}
