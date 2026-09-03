package com.example.locallife.product;

import org.junit.jupiter.api.Test;

import java.util.List;
import java.util.Optional;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

class ProductSearchFallbackTests {
    @Test
    void fallsBackToDatabaseWhenSearchPortSignalsUnavailable() {
        ProductRepository repository = mock(ProductRepository.class);
        ProductSearchPort search = mock(ProductSearchPort.class);
        when(search.search("phone", null, null, null, null, 20))
                .thenReturn(Optional.empty());
        when(repository.findByFilters("phone", null, null, null, null, 20))
                .thenReturn(List.of());
        ProductService service = new ProductService(
                repository,
                Optional.empty(),
                Optional.of(search)
        );

        assertThat(service.list("phone", null, null, null, null, null)).isEmpty();

        ProductSearchResult result = service.searchWithTrace(
                "phone", null, null, null, null, null
        );
        assertThat(result.channel()).isEqualTo("mysql");
        assertThat(result.degradedChannels()).containsExactly("elasticsearch");

        verify(repository, org.mockito.Mockito.times(2))
                .findByFilters("phone", null, null, null, null, 20);
    }
}
