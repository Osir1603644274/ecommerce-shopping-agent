package com.example.locallife.product;

import org.junit.jupiter.api.Test;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.jdbc.datasource.DriverManagerDataSource;
import java.util.HexFormat;
import java.util.List;
import java.util.UUID;
import static org.assertj.core.api.Assertions.*;

class ProductSourceResolutionControllerTests {
    private JdbcTemplate fixture() {
        var jdbc=new JdbcTemplate(new DriverManagerDataSource("jdbc:h2:mem:"+UUID.randomUUID()+";DB_CLOSE_DELAY=-1","sa",""));
        jdbc.execute("CREATE TABLE product(id BIGINT PRIMARY KEY,source VARCHAR(64),source_item_id VARCHAR(128),lifecycle_status VARCHAR(32))");
        jdbc.execute("CREATE TABLE external_catalog_identity(product_id BIGINT PRIMARY KEY,raw_sha BINARY(32))");
        for(long id:List.of(101L,102L,103L)) {
            jdbc.update("INSERT INTO product VALUES(?,?,?,?)",id,id==102?"multicpr":"kuaisearch","1",id==103?"ARCHIVED":"ACTIVE");
            jdbc.update("INSERT INTO external_catalog_identity VALUES(?,?)",id,HexFormat.of().parseHex("a".repeat(64)));
        }
        return jdbc;
    }
    @Test void resolvesExactSourceAndHashAndOmitsArchived() {
        var controller=new ProductSourceResolutionController(fixture());
        var rows=controller.resolve(new ProductSourceResolutionController.Request(List.of(
            new ProductSourceResolutionController.Identity("kuaisearch","1","a".repeat(64))))).data();
        assertThat(rows).hasSize(1);
        assertThat(rows.get(0).productId()).isEqualTo("101");
        assertThat(rows.get(0).rawSha256()).isEqualTo("a".repeat(64));
    }
    @Test void changedOrUnknownRecordHasNoTradingMapping() {
        var controller=new ProductSourceResolutionController(fixture());
        assertThat(controller.resolve(new ProductSourceResolutionController.Request(List.of(
            new ProductSourceResolutionController.Identity("kuaisearch","1","b".repeat(64)),
            new ProductSourceResolutionController.Identity("multicpr","2","a".repeat(64))))).data()).isEmpty();
    }
    @Test void requestBoundsAndIdentityFormatAreValidated() {
        try(var factory=jakarta.validation.Validation.buildDefaultValidatorFactory()) {
            var validator=factory.getValidator();
            assertThat(validator.validate(new ProductSourceResolutionController.Request(List.of()))).isNotEmpty();
            assertThat(validator.validate(new ProductSourceResolutionController.Request(List.of(
                new ProductSourceResolutionController.Identity("foreign","01","not-a-hash"))))).isNotEmpty();
        }
    }
}
