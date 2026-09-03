package com.example.locallife.product;

import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.stereotype.Service;

import java.util.List;
import java.util.Optional;

@Service
public class ProductService {
    private static final int DEFAULT_LIMIT = 20;
    private static final int MAX_LIMIT = 1500;
    private static final int CACHE_REBUILD_WAIT_ATTEMPTS = 3;
    private static final long CACHE_REBUILD_WAIT_MILLIS = 50L;
    private final ProductRepository repository;
    private final Optional<ProductCache> cache;
    private final Optional<ProductSearchPort> search;

    public ProductService(ProductRepository repository, Optional<ProductCache> cache) {
        this(repository, cache, Optional.empty());
    }

    @Autowired
    public ProductService(
            ProductRepository repository,
            Optional<ProductCache> cache,
            Optional<ProductSearchPort> search
    ) {
        this.repository = repository;
        this.cache = cache;
        this.search = search;
    }

    public List<ProductSummaryResponse> list(
            String query,
            String category,
            String brand,
            Long minPriceMinor,
            Long maxPriceMinor,
            Integer limit
    ) {
        return searchWithTrace(
                query, category, brand, minPriceMinor, maxPriceMinor, limit
        ).products();
    }

    public ProductSearchResult searchWithTrace(
            String query,
            String category,
            String brand,
            Long minPriceMinor,
            Long maxPriceMinor,
            Integer limit
    ) {
        String normalizedQuery = normalize(query);
        String normalizedCategory = normalize(category);
        String normalizedBrand = normalize(brand);
        int normalizedLimit = normalizeLimit(limit);
        if (normalizedQuery != null && search.isPresent()) {
            Optional<List<Long>> searchIds = search.get().search(
                    normalizedQuery,
                    normalizedCategory,
                    normalizedBrand,
                    minPriceMinor,
                    maxPriceMinor,
                    normalizedLimit
            );
            if (searchIds.isPresent()) {
                List<Long> recalled = searchIds.get();
                List<ProductSummaryResponse> products = recalled.stream()
                        .map(repository::findById)
                        .flatMap(Optional::stream)
                        .filter(this::hasAuthoritativeCommerceFacts)
                        .filter(product -> matchesHardConstraints(
                                product,
                                normalizedCategory,
                                normalizedBrand,
                                minPriceMinor,
                                maxPriceMinor
                        ))
                        .map(this::toSummary)
                        .toList();
                return new ProductSearchResult(
                        products, "elasticsearch", List.of(), recalled.size(),
                        products.size(), "mysql_product_inventory"
                );
            }
        }
        boolean controlledSearchFallback = normalizedQuery != null && search.isPresent();
        List<ProductSummaryResponse> products = repository.findByFilters(
                        normalizedQuery,
                        normalizedCategory,
                        normalizedBrand,
                        minPriceMinor,
                        maxPriceMinor,
                        normalizedLimit)
                .stream()
                .filter(product -> !controlledSearchFallback || hasAuthoritativeCommerceFacts(product))
                .map(this::toSummary)
                .toList();
        List<String> degraded = normalizedQuery != null && search.isPresent()
                ? List.of("elasticsearch")
                : List.of();
        return new ProductSearchResult(
                products, "mysql", degraded, products.size(), products.size(),
                "mysql_structured_fallback"
        );
    }

    public Optional<ProductDetailResponse> get(Long id) {
        if (cache.isEmpty()) {
            return queryProduct(id);
        }

        ProductCache productCache = cache.get();
        ProductCacheLookup lookup = productCache.getDetail(id);
        if (lookup.isHit()) {
            return Optional.of(lookup.detail());
        }
        if (lookup.isEmpty()) {
            return Optional.empty();
        }

        Optional<String> lockToken = productCache.tryLockDetail(id);
        if (lockToken.isEmpty()) {
            return waitForCacheRebuild(id, productCache);
        }
        try {
            ProductCacheLookup latest = productCache.getDetail(id);
            if (latest.isHit()) {
                return Optional.of(latest.detail());
            }
            if (latest.isEmpty()) {
                return Optional.empty();
            }
            Optional<ProductDetailResponse> result = queryProduct(id);
            if (result.isPresent()) {
                productCache.putDetail(id, result.get());
            } else {
                productCache.putEmpty(id);
            }
            return result;
        } finally {
            productCache.unlockDetail(id, lockToken.get());
        }
    }

    public List<ProductDetailResponse> resolve(List<Long> ids) {
        return ids.stream().distinct().map(this::get).flatMap(Optional::stream).toList();
    }

    private Optional<ProductDetailResponse> waitForCacheRebuild(Long id, ProductCache productCache) {
        for (int attempt = 0; attempt < CACHE_REBUILD_WAIT_ATTEMPTS; attempt++) {
            if (!sleepBeforeRetry()) {
                break;
            }
            ProductCacheLookup lookup = productCache.getDetail(id);
            if (lookup.isHit()) {
                return Optional.of(lookup.detail());
            }
            if (lookup.isEmpty()) {
                return Optional.empty();
            }
        }
        return queryProduct(id);
    }

    private Optional<ProductDetailResponse> queryProduct(Long id) {
        return repository.findById(id)
                .filter(product -> "ACTIVE".equalsIgnoreCase(product.lifecycleStatus()))
                .map(this::toDetail);
    }

    private boolean sleepBeforeRetry() {
        try {
            Thread.sleep(CACHE_REBUILD_WAIT_MILLIS);
            return true;
        } catch (InterruptedException exception) {
            Thread.currentThread().interrupt();
            return false;
        }
    }

    private ProductSummaryResponse toSummary(Product product) {
        return new ProductSummaryResponse(
                product.id(), product.source(), product.sourceItemId(), product.title(), product.brand(),
                product.seller(), product.categoryL1(), product.categoryL2(), product.categoryL3(),
                product.snapshotPriceMinor(), product.currency(), product.priceStatus(),
                product.lifecycleStatus(), product.entityVersion(), product.attributeText(),
                product.dataNature(), product.datasetRevision(), product.sourceLicense(),
                product.provenanceUrl(), product.importedAt());
    }

    private ProductDetailResponse toDetail(Product product) {
        ProductCommerceFacts commerce = repository.findCommerceFacts(product.id()).orElse(null);
        List<ProductAttributeResponse> attributes = repository.findAttributes(product.id()).stream()
                .map(attribute -> new ProductAttributeResponse(
                        attribute.key(), attribute.valueType(), attribute.rawValue(), attribute.normalizedText(),
                        attribute.normalizedNumber(), attribute.normalizedBoolean(), attribute.unit(),
                        attribute.evidenceField(), attribute.extractionMethod(), attribute.confidence()))
                .toList();
        return new ProductDetailResponse(
                product.id(), product.source(), product.sourceItemId(), product.title(), product.brand(),
                product.seller(), product.categoryL1(), product.categoryL2(), product.categoryL3(),
                product.snapshotPriceMinor(), product.currency(), product.priceStatus(),
                product.lifecycleStatus(), product.entityVersion(),
                commerce == null ? null : commerce.availableQuantity(),
                commerce == null ? null : commerce.inventoryVersion(),
                product.attributeText(),
                product.dataNature(), product.datasetRevision(), product.sourceLicense(),
                product.provenanceUrl(), product.importedAt(), attributes);
    }

    private String normalize(String value) {
        return value == null || value.isBlank() ? null : value.strip();
    }

    private int normalizeLimit(Integer limit) {
        return limit == null || limit <= 0 ? DEFAULT_LIMIT : Math.min(limit, MAX_LIMIT);
    }

    private static boolean matchesHardConstraints(
            Product product,
            String category,
            String brand,
            Long minPriceMinor,
            Long maxPriceMinor
    ) {
        if (!"ACTIVE".equalsIgnoreCase(product.lifecycleStatus())) {
            return false;
        }
        if (brand != null && (product.brand() == null
                || !product.brand().equalsIgnoreCase(brand))) {
            return false;
        }
        if (category != null && !categoryMatches(product, category)) {
            return false;
        }
        if (minPriceMinor == null && maxPriceMinor == null) {
            return true;
        }
        if (!"verified".equalsIgnoreCase(product.priceStatus())
                || product.snapshotPriceMinor() == null) {
            return false;
        }
        return (minPriceMinor == null || product.snapshotPriceMinor() >= minPriceMinor)
                && (maxPriceMinor == null || product.snapshotPriceMinor() <= maxPriceMinor);
    }

    private boolean hasAuthoritativeCommerceFacts(Product product) {
        return repository.findCommerceFacts(product.id())
                .filter(facts -> facts.entityVersion().equals(product.entityVersion()))
                .filter(facts -> "ACTIVE".equalsIgnoreCase(facts.lifecycleStatus()))
                .filter(facts -> "verified".equalsIgnoreCase(facts.priceStatus()))
                .filter(facts -> facts.snapshotPriceMinor() != null)
                .filter(facts -> facts.availableQuantity() != null && facts.availableQuantity() > 0)
                .isPresent();
    }

    private static boolean categoryMatches(Product product, String category) {
        return category.equals(product.categoryL1())
                || contains(product.categoryL2(), category)
                || contains(product.categoryL3(), category);
    }

    private static boolean contains(String value, String expected) {
        return value != null && value.contains(expected);
    }
}
