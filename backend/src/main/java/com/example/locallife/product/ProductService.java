package com.example.locallife.product;

import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.stereotype.Service;

import java.util.List;
import java.util.Optional;
import java.util.Map;
import java.util.Set;
import java.util.Objects;
import java.util.function.Function;
import java.util.stream.Collectors;

@Service
public class ProductService {
    private static final int DEFAULT_LIMIT = 20;
    private static final int MAX_LIMIT = 1500;
    private static final int CACHE_REBUILD_WAIT_ATTEMPTS = 3;
    private static final long CACHE_REBUILD_WAIT_MILLIS = 50L;
    private final ProductRepository repository;
    private final Optional<ProductCache> cache;
    private final Optional<ProductSearchPort> search;
    private LocalOfferService localOffers;

    public ProductService(ProductRepository repository, Optional<ProductCache> cache) {
        this(repository, cache, Optional.empty());
    }

    public ProductService(
            ProductRepository repository,
            Optional<ProductCache> cache,
            Optional<ProductSearchPort> search
    ) {
        this.repository = repository;
        this.cache = cache;
        this.search = search;
    }

    @Autowired
    public ProductService(ProductRepository repository,Optional<ProductCache> cache,
            Optional<ProductSearchPort> search,LocalOfferService localOffers) {
        this(repository,cache,search); this.localOffers=localOffers;
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
        return searchWithTrace(query,category,brand,minPriceMinor,maxPriceMinor,limit,null);
    }

    public ProductSearchResult searchWithTrace(String query,String category,String brand,
            Long minPriceMinor,Long maxPriceMinor,Integer limit,String catalogVersion) {
        String normalizedQuery = normalize(query);
        String normalizedCategory = normalize(category);
        String normalizedBrand = normalize(brand);
        int normalizedLimit = normalizeLimit(limit);
        String version = normalize(catalogVersion);
        Set<Long> members = version == null ? null : repository.findPublishedByIdAfter(version,0L,MAX_LIMIT)
                .stream().map(Product::id).collect(Collectors.toSet());
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
                List<Long> recalled = searchIds.get().stream()
                        .filter(id -> members == null || members.contains(id)).toList();
                Map<Long, Product> byId = repository.findByIds(recalled).stream()
                        .collect(Collectors.toMap(Product::id, Function.identity()));
                // SQL IN does not preserve ES rank; restore it, including repeated IDs.
                List<Product> ordered = recalled.stream().map(byId::get).filter(Objects::nonNull).toList();
                List<ProductSummaryResponse> products = authoritativeProducts(ordered).stream()
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
        List<Product> candidates = version != null
                ? repository.findByCatalogFilters(version,normalizedQuery,normalizedCategory,normalizedBrand,
                        minPriceMinor,maxPriceMinor,normalizedLimit)
                : repository.findByFilters(
                        normalizedQuery,
                        normalizedCategory,
                        normalizedBrand,
                        minPriceMinor,
                        maxPriceMinor,
                        normalizedLimit);
        List<ProductSummaryResponse> products = (controlledSearchFallback
                ? authoritativeProducts(candidates) : candidates).stream()
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
        List<Long> unique = ids.stream().distinct().toList();
        if (unique.isEmpty()) return List.of();
        Map<Long, Product> products = repository.findByIds(unique).stream()
                .filter(p -> !"DELETED".equalsIgnoreCase(p.lifecycleStatus()))
                .collect(Collectors.toMap(Product::id, Function.identity()));
        List<Long> present = unique.stream().filter(products::containsKey).toList();
        if (present.isEmpty()) return List.of();
        // This endpoint verifies a recalled candidate batch. Read inventory once
        // for the batch, not once per product over the remote service. Do not
        // treat a dependency failure as empty/zero stock or use stale cache.
        Map<Long, ProductCommerceFacts> facts = repository.findCommerceFactsByIds(present).stream()
                .collect(Collectors.toMap(ProductCommerceFacts::productId, Function.identity()));
        return present.stream().map(id -> toDetail(products.get(id), facts.get(id))).toList();
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
                .filter(product -> !"DELETED".equalsIgnoreCase(product.lifecycleStatus()))
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
        return toDetail(product, commerce);
    }

    private ProductDetailResponse toDetail(Product product, ProductCommerceFacts commerce) {
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

    private List<Product> authoritativeProducts(List<Product> products) {
        if (products.isEmpty()) return List.of();
        List<Long> activeIds = products.stream().filter(p -> "ACTIVE".equals(p.lifecycleStatus()))
                .map(Product::id).distinct().toList();
        Set<Long> offered = localOffers == null || activeIds.isEmpty()
                ? Set.of() : localOffers.findProductIds(activeIds);
        List<Long> remaining = products.stream()
                .filter(p -> !("ACTIVE".equals(p.lifecycleStatus()) && offered.contains(p.id())))
                .map(Product::id).distinct().toList();
        Map<Long, ProductCommerceFacts> factsById = remaining.isEmpty() ? Map.of()
                : repository.findCommerceFactsByIds(remaining).stream()
                    .collect(Collectors.toMap(ProductCommerceFacts::productId, Function.identity()));
        return products.stream().filter(product -> {
            // Sold-out local offers stay searchable; checkout checks current stock.
            if ("ACTIVE".equals(product.lifecycleStatus()) && offered.contains(product.id())) return true;
            return Optional.ofNullable(factsById.get(product.id()))
                .filter(facts -> facts.entityVersion().equals(product.entityVersion()))
                .filter(facts -> "ACTIVE".equalsIgnoreCase(facts.lifecycleStatus()))
                .filter(facts -> "verified".equalsIgnoreCase(facts.priceStatus()))
                .filter(facts -> facts.snapshotPriceMinor() != null)
                .filter(facts -> facts.availableQuantity() != null && facts.availableQuantity() > 0)
                .isPresent();
        }).toList();
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
