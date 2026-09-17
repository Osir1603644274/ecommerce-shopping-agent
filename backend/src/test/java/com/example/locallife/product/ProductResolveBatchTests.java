package com.example.locallife.product;

import org.junit.jupiter.api.Test;
import java.util.List;
import java.util.Optional;
import java.util.stream.LongStream;
import static org.assertj.core.api.Assertions.*;
import static org.mockito.Mockito.*;

class ProductResolveBatchTests {
    Product product(long id, String status) {
        return new Product(id,"fixture","p"+id,"phone","Apple","seller","phone","phone","二手手机",
            10000L,"CNY","verified",status,1L,"","test","v1","MIT","",null);
    }

    @Test void tenColdProductsUseOneInventoryBatchNotTenRemoteCalls() {
        var repository=mock(ProductRepository.class);
        var cache=mock(ProductCache.class);
        var ids=LongStream.rangeClosed(1,10).boxed().toList();
        when(repository.findByIds(ids)).thenReturn(ids.stream().sorted(java.util.Comparator.reverseOrder()).map(id->product(id,"ACTIVE")).toList());
        when(repository.findCommerceFactsByIds(ids)).thenReturn(ids.stream().map(id->
            new ProductCommerceFacts(id,10000L,"verified","ACTIVE",1L,7,2L)).toList());
        var result=new ProductService(repository,Optional.of(cache)).resolve(ids);
        assertThat(result).extracting(ProductDetailResponse::id).containsExactlyElementsOf(ids);
        assertThat(result).allSatisfy(p->{assertThat(p.availableQuantity()).isEqualTo(7);assertThat(p.inventoryVersion()).isEqualTo(2L);});
        verify(repository,times(1)).findCommerceFactsByIds(ids);
        verify(repository,never()).findCommerceFacts(anyLong());
        verifyNoInteractions(cache);
    }

    @Test void deduplicatesOmitsMissingAndDeletedWithoutInventingStock() {
        var repository=mock(ProductRepository.class);
        when(repository.findByIds(List.of(2L,1L,3L,4L))).thenReturn(List.of(product(1,"DELETED"),product(2,"ACTIVE"),product(3,"ACTIVE")));
        when(repository.findCommerceFactsByIds(List.of(2L,3L))).thenReturn(List.of());
        var result=new ProductService(repository,Optional.empty()).resolve(List.of(2L,1L,2L,3L,4L));
        assertThat(result).extracting(ProductDetailResponse::id).containsExactly(2L,3L);
        assertThat(result).allSatisfy(p->assertThat(p.availableQuantity()).isNull());
    }

    @Test void inventoryFailureRemainsFailureNotAnEmptySearch() {
        var repository=mock(ProductRepository.class);
        when(repository.findByIds(List.of(1L))).thenReturn(List.of(product(1,"ACTIVE")));
        when(repository.findCommerceFactsByIds(List.of(1L))).thenThrow(new IllegalStateException("inventory_service_unavailable"));
        assertThatThrownBy(()->new ProductService(repository,Optional.empty()).resolve(List.of(1L)))
            .hasMessage("inventory_service_unavailable");
    }

    @Test void emptyBatchDoesNotReadDatabaseOrRemoteInventory() {
        var repository=mock(ProductRepository.class);
        assertThat(new ProductService(repository,Optional.empty()).resolve(List.of())).isEmpty();
        verifyNoInteractions(repository);
    }
}
