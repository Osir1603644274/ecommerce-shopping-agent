package com.example.locallife.product;

import org.junit.jupiter.api.Test;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.jdbc.datasource.DriverManagerDataSource;
import java.util.Optional;
import java.util.UUID;
import static org.mockito.Mockito.*;
import static org.assertj.core.api.Assertions.*;

class ProductOfferAbsenceTests {
    @Test void missingOfferAndSnapshotReturnUnavailableInsteadOfNullUnboxing() { check("missing",null,null,null,false); }
    @Test void verifiedButMissingSnapshotStillReturnsUnavailable() { check("verified",null,null,null,false); }
    @Test void verifiedSnapshotRemainsAvailable() { check("verified",1200L,null,1200L,true); }
    @Test void localOfferTakesPrecedenceWithoutPromotingSourcePrice() {
        check("missing",null,new LocalOfferService.Offer(1500,"CNY","local_simulated",1),1500L,true);
    }
    private void check(String state,Long snapshot,LocalOfferService.Offer offer,Long expected,boolean available) {
        var products=mock(ProductRepository.class);var offers=mock(LocalOfferService.class);var product=mock(Product.class);
        when(products.findById(7L)).thenReturn(Optional.of(product));when(offers.find(7L)).thenReturn(Optional.ofNullable(offer));
        when(product.priceStatus()).thenReturn(state);when(product.snapshotPriceMinor()).thenReturn(snapshot);
        when(product.lifecycleStatus()).thenReturn("ACTIVE");when(product.currency()).thenReturn("CNY");
        var jdbc=new JdbcTemplate(new DriverManagerDataSource("jdbc:h2:mem:"+UUID.randomUUID()+";DB_CLOSE_DELAY=-1","sa",""));
        jdbc.execute("CREATE TABLE inventory_stock(item_type VARCHAR(32),item_id BIGINT,available_quantity INT)");
        jdbc.execute("CREATE TABLE external_catalog_identity(product_id BIGINT)");
        jdbc.execute("INSERT INTO inventory_stock VALUES('PRODUCT',7,10)");
        var result=new ProductOfferController(products,offers,jdbc).get(7).data();
        assertThat(result.priceMinor()).isEqualTo(expected);assertThat(result.canPurchase()).isEqualTo(available);
        assertThat(result.externalCatalogEligible()).isFalse();
    }
}
