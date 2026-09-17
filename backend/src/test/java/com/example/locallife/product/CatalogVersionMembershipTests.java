package com.example.locallife.product;
import org.junit.jupiter.api.Test;
import java.time.Instant;
import java.util.List;
import static org.mockito.Mockito.*;
import static org.assertj.core.api.Assertions.*;

class CatalogVersionMembershipTests {
    @Test void publishedViewDoesNotExpandWhenTradingCatalogGrows() {
        var states=mock(CatalogStateMapper.class);var products=mock(ProductMapper.class);
        when(states.findLatest()).thenReturn(new CatalogState("frozen",3,"hash",Instant.EPOCH));
        when(states.findMemberIds("frozen",0,3)).thenReturn(List.of(1L,2L,3L));
        var page=new CatalogVersionRepository(states,products).getProducts("frozen",null,2);
        assertThat(page.items()).containsExactly(1L,2L);
        assertThat(page.complete()).isFalse();assertThat(page.nextAfterId()).isEqualTo(2L);
        verifyNoInteractions(products);
    }
}
