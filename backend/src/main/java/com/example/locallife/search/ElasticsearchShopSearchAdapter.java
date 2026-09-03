package com.example.locallife.search;

import com.example.locallife.shop.ShopSearchPort;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.stereotype.Component;

import java.util.List;
import java.util.Optional;

@Component
@ConditionalOnProperty(prefix = "local-life.search", name = "enabled", havingValue = "true")
class ElasticsearchShopSearchAdapter implements ShopSearchPort {
    private final ElasticsearchGateway gateway;

    ElasticsearchShopSearchAdapter(ElasticsearchGateway gateway) {
        this.gateway = gateway;
    }

    @Override
    public Optional<List<Long>> search(Long typeId, String name, int limit) {
        return gateway.searchShops(typeId, name, limit);
    }
}
