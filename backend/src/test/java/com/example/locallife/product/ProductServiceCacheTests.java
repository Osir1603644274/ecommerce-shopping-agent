package com.example.locallife.product;

import org.junit.jupiter.api.Test;

import java.time.LocalDateTime;
import java.util.List;
import java.util.Optional;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.verifyNoInteractions;
import static org.mockito.Mockito.when;

class ProductServiceCacheTests {
    private final ProductRepository repository = mock(ProductRepository.class);
    private final ProductCache cache = mock(ProductCache.class);
    private final ProductService service = new ProductService(repository, Optional.of(cache));

    @Test
    void cacheHitDoesNotQueryMysql() {
        ProductDetailResponse detail = detail();
        when(cache.getDetail(1001L)).thenReturn(ProductCacheLookup.hit(detail));

        assertThat(service.get(1001L)).contains(detail);
        verifyNoInteractions(repository);
    }

    @Test
    void cacheMissQueriesMysqlAndCachesAuthoritativeDetail() {
        Product product = product();
        when(cache.getDetail(1001L)).thenReturn(ProductCacheLookup.miss());
        when(cache.tryLockDetail(1001L)).thenReturn(Optional.of("product-lock"));
        when(repository.findById(1001L)).thenReturn(Optional.of(product));
        when(repository.findAttributes(1001L)).thenReturn(List.of());

        ProductDetailResponse result = service.get(1001L).orElseThrow();

        assertThat(result.id()).isEqualTo(1001L);
        verify(repository).findById(1001L);
        verify(cache).putDetail(1001L, result);
        verify(cache, never()).putEmpty(1001L);
        verify(cache).unlockDetail(1001L, "product-lock");
    }

    @Test
    void emptySentinelDoesNotQueryMysql() {
        when(cache.getDetail(999L)).thenReturn(ProductCacheLookup.empty());

        assertThat(service.get(999L)).isEmpty();
        verifyNoInteractions(repository);
    }

    private Product product() {
        return new Product(
                1001L, "kuaisearch", "fixture-phone-1", "测试手机", "测试品牌",
                "测试卖家", "手机/数码/电脑办公", "手机通讯", "智能手机",
                299900L, "CNY", "verified", "ACTIVE", 1L,
                "12GB内存", "test_fixture",
                "fixture-v1", "MIT",
                "https://huggingface.co/datasets/benchen4395/KuaiSearch",
                LocalDateTime.of(2026, 7, 28, 0, 0)
        );
    }

    private ProductDetailResponse detail() {
        Product product = product();
        return new ProductDetailResponse(
                product.id(), product.source(), product.sourceItemId(), product.title(),
                product.brand(), product.seller(), product.categoryL1(), product.categoryL2(),
                product.categoryL3(), product.snapshotPriceMinor(), product.currency(),
                product.priceStatus(), product.lifecycleStatus(), product.entityVersion(),
                null, null,
                product.attributeText(), product.dataNature(),
                product.datasetRevision(), product.sourceLicense(), product.provenanceUrl(),
                product.importedAt(), List.of()
        );
    }
}
