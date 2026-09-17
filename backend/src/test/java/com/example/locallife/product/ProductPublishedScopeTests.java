package com.example.locallife.product;

import org.junit.jupiter.api.Test;
import java.util.*;
import static org.assertj.core.api.Assertions.*;
import static org.mockito.Mockito.*;

class ProductPublishedScopeTests {
    @Test void sqlScopeIsBoundAsAParameterAndDoesNotExpandToFullCatalog() {
        String sql=ProductSqlProvider.findByFilters(Map.of("catalogVersion","frozen"),null);
        assertThat(sql).contains("catalog_version=#{catalogVersion}").doesNotContain("frozen");
        var repository=mock(ProductRepository.class);
        when(repository.findPublishedByIdAfter("frozen",0L,1500)).thenReturn(List.of());
        when(repository.findByCatalogFilters("frozen",null,"手机",null,null,null,1500)).thenReturn(List.of());
        var service=new ProductService(repository,Optional.empty());
        assertThat(service.searchWithTrace(null,"手机",null,null,null,1500,"frozen").products()).isEmpty();
        verify(repository).findByCatalogFilters("frozen",null,"手机",null,null,null,1500);
        verify(repository,never()).findByFilters(any(),any(),any(),any(),any(),anyInt());
    }
    @Test void elasticsearchCannotIntroduceAnUnpublishedExternalProduct() {
        var repository=mock(ProductRepository.class);var search=mock(ProductSearchPort.class);
        when(repository.findPublishedByIdAfter("frozen",0L,1500)).thenReturn(List.of());
        when(search.search("手机",null,null,null,null,20)).thenReturn(Optional.of(List.of(4000000000000001L)));
        when(repository.findByIds(List.of())).thenReturn(List.of());
        var service=new ProductService(repository,Optional.empty(),Optional.of(search));
        assertThat(service.searchWithTrace("手机",null,null,null,null,20,"frozen").products()).isEmpty();
        verify(repository).findByIds(List.of());
    }
}
