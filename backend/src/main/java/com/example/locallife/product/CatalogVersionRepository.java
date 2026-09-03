package com.example.locallife.product;

import org.springframework.stereotype.Repository;
import org.apache.ibatis.annotations.Mapper;
import org.apache.ibatis.annotations.Param;
import org.apache.ibatis.annotations.Select;
import org.apache.ibatis.annotations.Insert;

import java.time.Instant;
import java.util.List;
import java.util.Optional;

/** Row from the catalog_state table — one published catalog version. */
record CatalogState(
    String catalogVersion,
    int productCount,
    String contentHash,
    Instant publishedAt
) {}

@Mapper
interface CatalogStateMapper {

    @Select("""
        SELECT catalog_version, product_count, content_hash, published_at
        FROM catalog_state
        ORDER BY published_at DESC
        LIMIT 1
        """)
    CatalogState findLatest();

    @Insert("""
        INSERT INTO catalog_state (catalog_version, product_count, content_hash)
        VALUES (#{catalogVersion}, #{productCount}, #{contentHash})
        """)
    int insert(@Param("catalogVersion") String catalogVersion,
               @Param("productCount") int productCount,
               @Param("contentHash") String contentHash);
}

/** Lightweight response for the internal catalog manifest endpoint. */
record CatalogManifestResponse(
    String catalogVersion,
    int productCount,
    String contentHash,
    Instant publishedAt
) {
    static CatalogManifestResponse from(CatalogState state) {
        return new CatalogManifestResponse(
            state.catalogVersion(), state.productCount(),
            state.contentHash(), state.publishedAt());
    }
}

/** One page of product IDs from a specific catalog version. */
record CatalogPageResponse(
    String catalogVersion,
    int productCount,
    String contentHash,
    List<Long> items,
    Long nextAfterId,
    boolean complete
) {}

@Repository
class CatalogVersionRepository {
    private final CatalogStateMapper stateMapper;
    private final ProductMapper productMapper;

    CatalogVersionRepository(CatalogStateMapper stateMapper, ProductMapper productMapper) {
        this.stateMapper = stateMapper;
        this.productMapper = productMapper;
    }

    Optional<CatalogManifestResponse> getLatestManifest() {
        CatalogState latest = stateMapper.findLatest();
        return Optional.ofNullable(latest).map(CatalogManifestResponse::from);
    }

    CatalogPageResponse getProducts(String catalogVersion, Long afterId, int limit) {
        CatalogState state = stateMapper.findLatest();
        if (state == null || !state.catalogVersion().equals(catalogVersion)) {
            // Version mismatch — caller must rebuild
            return new CatalogPageResponse(
                catalogVersion, 0, "", List.of(), null, false);
        }
        List<Product> products = productMapper.findByIdAfter(
            afterId != null ? afterId : 0L, limit + 1);
        boolean hasMore = products.size() > limit;
        if (hasMore) {
            products = products.subList(0, limit);
        }
        List<Long> ids = products.stream().map(Product::id).toList();
        Long nextAfter = ids.isEmpty() ? null : ids.get(ids.size() - 1);
        return new CatalogPageResponse(
            catalogVersion, state.productCount(), state.contentHash(),
            ids, nextAfter, !hasMore);
    }
}
