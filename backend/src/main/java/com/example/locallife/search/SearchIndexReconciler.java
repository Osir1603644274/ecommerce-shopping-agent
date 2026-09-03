package com.example.locallife.search;

import com.example.locallife.product.ProductRepository;
import com.example.locallife.shop.ShopRepository;
import io.micrometer.core.instrument.MeterRegistry;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Component;

import java.util.List;
import com.example.locallife.product.Product;

@Component
@ConditionalOnProperty(prefix = "local-life.search", name = "enabled", havingValue = "true")
class SearchIndexReconciler {
    private static final Logger log = LoggerFactory.getLogger(SearchIndexReconciler.class);
    private final ElasticsearchGateway gateway;
    private final ProductRepository productRepository;
    private final ShopRepository shopRepository;
    private final SearchProperties properties;
    private final MeterRegistry meters;

    SearchIndexReconciler(
            ElasticsearchGateway gateway,
            ProductRepository productRepository,
            ShopRepository shopRepository,
            SearchProperties properties,
            MeterRegistry meters
    ) {
        this.gateway = gateway;
        this.productRepository = productRepository;
        this.shopRepository = shopRepository;
        this.properties = properties;
        this.meters = meters;
    }

    @Scheduled(
            initialDelayString = "${local-life.search.initial-reconcile-delay:PT10S}",
            fixedDelayString = "${local-life.search.reconcile-delay:PT5M}"
    )
    void reconcile() {
        try {
            gateway.ensureIndices();
        } catch (RuntimeException exception) {
            recordFailure("indices", exception);
            return;
        }
        reconcileProducts();
        reconcileShops();
    }

    private void reconcileProducts() {
        try {
            long afterId = 0L;
            while (true) {
                List<Product> page = productRepository.findByIdAfter(
                        afterId, properties.reconcileLimit());
                if (page.isEmpty()) {
                    break;
                }
                gateway.indexProducts(page.stream()
                        .filter(product -> "ACTIVE".equalsIgnoreCase(product.lifecycleStatus()))
                        .toList());
                page.stream()
                        .filter(product -> "DELETED".equalsIgnoreCase(product.lifecycleStatus()))
                        .forEach(product -> gateway.deleteProduct(product.id(), product.entityVersion()));
                afterId = page.get(page.size() - 1).id();
                if (page.size() < properties.reconcileLimit()) {
                    break;
                }
            }
            recordSuccess("product");
        } catch (RuntimeException exception) {
            recordFailure("product", exception);
        }
    }

    private void reconcileShops() {
        try {
            gateway.indexShops(shopRepository.findAll());
            recordSuccess("shop");
        } catch (RuntimeException exception) {
            recordFailure("shop", exception);
        }
    }

    private void recordSuccess(String entity) {
        meters.counter(
                "local_life.search.reconcile",
                "entity", entity,
                "outcome", "success"
        ).increment();
    }

    private void recordFailure(String entity, RuntimeException exception) {
        meters.counter(
                "local_life.search.reconcile",
                "entity", entity,
                "outcome", "failure"
        ).increment();
        log.warn("Search index reconciliation failed for {}; database search remains available",
                    entity,
                    exception);
    }
}
