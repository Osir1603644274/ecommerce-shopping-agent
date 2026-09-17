package com.example.locallife.review;

import com.example.locallife.shop.ShopRepository;
import org.junit.jupiter.api.Test;
import org.springframework.jdbc.core.JdbcTemplate;
import java.util.Optional;
import static org.assertj.core.api.Assertions.*;
import static org.mockito.Mockito.*;
import static org.mockito.ArgumentMatchers.*;

class ReviewProjectionSourceTests {
    @Test
    void oldUpsertEventSnapshotsDeletionInsteadOfItsOldPayload() {
        var jdbc=mock(JdbcTemplate.class);
        var reviews=mock(ReviewRepository.class);
        var shops=mock(ShopRepository.class);
        when(jdbc.queryForObject(anyString(),eq(Long.class),eq("r"))).thenReturn(7L);
        when(reviews.findById("r")).thenReturn(Optional.empty());
        var snapshot=new ReviewProjectionSource(jdbc,reviews,shops).snapshot("r");
        assertThat(snapshot.revision()).isEqualTo(8);
        assertThat(snapshot.review()).isNull();
        verify(jdbc).update("UPDATE review_projection_head SET revision=? WHERE review_id=?",8L,"r");
        verifyNoInteractions(shops);
    }
}
