package com.example.locallife.search;

import org.junit.jupiter.api.Test;
import org.springframework.jdbc.core.JdbcTemplate;
import static org.mockito.Mockito.*;
import static org.assertj.core.api.Assertions.*;

class CacheInvalidationPublisherTests {
    @Test void preservesFailureSoTheMutationCannotCommitWithoutItsInvalidation() {
        var jdbc=mock(JdbcTemplate.class);
        when(jdbc.update(anyString(),any(),any(),any())).thenThrow(new IllegalStateException("db down"));
        assertThatThrownBy(()->new CacheInvalidationPublisher(jdbc).productUpdated(2L))
                .isInstanceOf(IllegalStateException.class);
    }
}
