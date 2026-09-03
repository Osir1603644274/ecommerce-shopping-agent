package com.example.locallife.product;

import org.junit.jupiter.api.Test;

import java.time.LocalDateTime;
import java.util.List;
import java.util.Optional;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.Mockito.*;

class ProductAuthorityFilteringTests {
    @Test
    void elasticsearchCandidatesMustPassMysqlPriceStockAndLifecycleFacts() {
        ProductRepository repository = mock(ProductRepository.class);
        ProductSearchPort search = mock(ProductSearchPort.class);
        Product active = product(1L, "ACTIVE", 3L);
        Product noStock = product(2L, "ACTIVE", 4L);
        when(search.search("phone", null, null, null, null, 20))
                .thenReturn(Optional.of(List.of(1L, 2L)));
        when(repository.findById(1L)).thenReturn(Optional.of(active));
        when(repository.findById(2L)).thenReturn(Optional.of(noStock));
        when(repository.findCommerceFacts(1L)).thenReturn(Optional.of(
                new ProductCommerceFacts(1L, 299900L, "verified", "ACTIVE", 3L, 1, 9L)));
        when(repository.findCommerceFacts(2L)).thenReturn(Optional.of(
                new ProductCommerceFacts(2L, 299900L, "verified", "ACTIVE", 4L, 0, 10L)));
        ProductService service = new ProductService(repository, Optional.empty(), Optional.of(search));

        ProductSearchResult result = service.searchWithTrace("phone", null, null, null, null, null);

        assertThat(result.products()).extracting(ProductSummaryResponse::id).containsExactly(1L);
        assertThat(result.recallCount()).isEqualTo(2);
        assertThat(result.authoritativeEligibleCount()).isEqualTo(1);
        assertThat(result.factAuthority()).isEqualTo("mysql_product_inventory");
    }

    private static Product product(Long id, String status, Long version) {
        return new Product(
                id, "source", "item-" + id, "测试手机", "品牌", "卖家",
                "手机", "手机通讯", "智能手机", 299900L, "CNY", "verified",
                status, version, "12GB", "fixture", "v1", "MIT", "https://example.test",
                LocalDateTime.of(2026, 8, 29, 0, 0)
        );
    }
}
