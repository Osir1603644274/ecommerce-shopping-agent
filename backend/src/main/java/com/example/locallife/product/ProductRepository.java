package com.example.locallife.product;

import org.springframework.stereotype.Repository;

import java.util.List;
import java.util.Optional;

@Repository
public class ProductRepository {
    private final ProductMapper mapper;

    public ProductRepository(ProductMapper mapper) {
        this.mapper = mapper;
    }

    public List<Product> findByFilters(
            String query,
            String category,
            String brand,
            Long minPriceMinor,
            Long maxPriceMinor,
            int limit
    ) {
        return mapper.findByFilters(query, category, brand, minPriceMinor, maxPriceMinor, limit);
    }

    public Optional<Product> findById(Long id) {
        return Optional.ofNullable(mapper.findById(id));
    }

    public int create(CreateProductRequest request) {
        return mapper.insert(request);
    }

    public List<ProductAttribute> findAttributes(Long productId) {
        return mapper.findAttributes(productId);
    }

    public List<Product> findByLifecycleStatus(String status, int limit) {
        return mapper.findByLifecycleStatus(status, limit);
    }

    public List<Product> findByIdAfter(long afterId, int limit) {
        return mapper.findByIdAfter(afterId, limit);
    }

    public Optional<ProductCommerceFacts> findCommerceFacts(Long productId) {
        return Optional.ofNullable(mapper.findCommerceFacts(productId));
    }

    public int update(Long productId, ProductUpdateRequest request) {
        return mapper.update(productId, request);
    }

    public int softDelete(Long productId, Long expectedVersion) {
        return mapper.softDelete(productId, expectedVersion);
    }
}
