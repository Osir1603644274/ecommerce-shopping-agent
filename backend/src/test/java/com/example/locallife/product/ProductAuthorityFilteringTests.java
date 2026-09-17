package com.example.locallife.product;

import org.junit.jupiter.api.Test;

import java.time.LocalDateTime;
import java.util.List;
import java.util.Optional;
import java.util.Set;

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
        when(repository.findByIds(List.of(1L, 2L))).thenReturn(List.of(active, noStock));
        when(repository.findCommerceFactsByIds(List.of(1L, 2L))).thenReturn(List.of(
                new ProductCommerceFacts(1L, 299900L, "verified", "ACTIVE", 3L, 1, 9L),
                new ProductCommerceFacts(2L, 299900L, "verified", "ACTIVE", 4L, 0, 10L)));
        ProductService service = new ProductService(repository, Optional.empty(), Optional.of(search));

        ProductSearchResult result = service.searchWithTrace("phone", null, null, null, null, null);

        assertThat(result.products()).extracting(ProductSummaryResponse::id).containsExactly(1L);
        assertThat(result.recallCount()).isEqualTo(2);
        assertThat(result.authoritativeEligibleCount()).isEqualTo(1);
        assertThat(result.factAuthority()).isEqualTo("mysql_product_inventory");
        verify(repository, never()).findById(anyLong());
        verify(repository, never()).findCommerceFacts(anyLong());
    }

    @Test
    void keepsRecallOrderDuplicatesAndSoldOutLocalOffersButRejectsStaleOrDeletedFacts() {
        ProductRepository repository = mock(ProductRepository.class);
        ProductSearchPort search = mock(ProductSearchPort.class);
        LocalOfferService offers = mock(LocalOfferService.class);
        List<Long> recalled = List.of(2L, 1L, 2L, 99L, 3L, 4L);
        when(search.search("phone", null, null, null, null, 20)).thenReturn(Optional.of(recalled));
        when(repository.findByIds(recalled)).thenReturn(List.of(
                product(1L,"ACTIVE",1L), product(2L,"ACTIVE",1L),
                product(3L,"ACTIVE",1L), product(4L,"DELETED",1L)));
        when(offers.findProductIds(List.of(2L,1L,3L))).thenReturn(Set.of(2L));
        when(repository.findCommerceFactsByIds(List.of(1L,3L,4L))).thenReturn(List.of(
                new ProductCommerceFacts(1L,299900L,"verified","ACTIVE",1L,1,1L),
                new ProductCommerceFacts(3L,299900L,"verified","ACTIVE",2L,1,1L),
                new ProductCommerceFacts(4L,299900L,"verified","DELETED",1L,1,1L)));
        ProductService service = new ProductService(repository,Optional.empty(),Optional.of(search),offers);
        assertThat(service.list("phone",null,null,null,null,20))
                .extracting(ProductSummaryResponse::id).containsExactly(2L,1L,2L);
        verify(repository,never()).findById(anyLong());
        verify(offers,never()).find(anyLong());
    }

    @Test
    void databaseFallbackStillRejectsMissingUnverifiedAndEmptyStockFacts() {
        ProductRepository repository = mock(ProductRepository.class);
        ProductSearchPort search = mock(ProductSearchPort.class);
        when(search.search("phone",null,null,null,null,20)).thenReturn(Optional.empty());
        when(repository.findByFilters("phone",null,null,null,null,20)).thenReturn(List.of(
                product(1L,"ACTIVE",1L),product(2L,"ACTIVE",1L),product(3L,"ACTIVE",1L),product(4L,"ACTIVE",1L)));
        when(repository.findCommerceFactsByIds(List.of(1L,2L,3L,4L))).thenReturn(List.of(
                new ProductCommerceFacts(1L,299900L,"verified","ACTIVE",1L,1,1L),
                new ProductCommerceFacts(2L,299900L,"unknown","ACTIVE",1L,1,1L),
                new ProductCommerceFacts(3L,299900L,"verified","ACTIVE",1L,0,1L)));
        ProductService service=new ProductService(repository,Optional.empty(),Optional.of(search));
        ProductSearchResult result=service.searchWithTrace("phone",null,null,null,null,20);
        assertThat(result.products()).extracting(ProductSummaryResponse::id).containsExactly(1L);
        assertThat(result.degradedChannels()).containsExactly("elasticsearch");
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
