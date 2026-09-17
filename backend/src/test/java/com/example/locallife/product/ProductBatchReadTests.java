package com.example.locallife.product;

import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.jdbc.datasource.SingleConnectionDataSource;
import java.util.List;
import java.util.stream.LongStream;
import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.Mockito.*;

@SpringBootTest
class ProductBatchReadTests {
    @Autowired ProductRepository repository;
    @Autowired JdbcTemplate jdbc;

    @Test
    void actualMybatisBatchQueriesMatchSingleReadsAndHandleEmptyInputs() {
        assertThat(repository.findByIds(List.of())).isEmpty();
        assertThat(repository.findCommerceFactsByIds(List.of())).isEmpty();
        List<Product> products=repository.findByIds(List.of(1003L,1001L,9999L,1001L));
        assertThat(products).containsExactlyInAnyOrder(repository.findById(1001L).orElseThrow(),repository.findById(1003L).orElseThrow());
        assertThat(repository.findCommerceFactsByIds(List.of(1001L,9999L,1001L)))
                .containsExactly(repository.findCommerceFacts(1001L).orElseThrow());
    }

    @Test
    void offerBatchMatchesExistingLookupAndHonorsDisabledFlagAndReadBudget() {
        // The general H2 commerce fixture does not enable the demo offer table.
        SingleConnectionDataSource source=new SingleConnectionDataSource("jdbc:h2:mem:offer_batch;MODE=MySQL","sa","",true);
        try {
            JdbcTemplate offerJdbc=new JdbcTemplate(source);
            offerJdbc.execute("CREATE TABLE product_local_offer(product_id BIGINT PRIMARY KEY,price_minor BIGINT,currency VARCHAR(16),price_kind VARCHAR(32),version BIGINT)");
            offerJdbc.update("INSERT INTO product_local_offer VALUES(1001,114600,'CNY','simulated',1)");
            LocalOfferService enabled=new LocalOfferService(offerJdbc,true);
            List<Long> ids=List.of(1001L,1002L);
            try(var budget=CatalogReadBudget.open(2)) {
                assertThat(enabled.findProductIds(ids)).containsExactly(1001L);
                assertThat(enabled.findProductIds(ids)).containsExactlyInAnyOrderElementsOf(
                        ids.stream().filter(id->enabled.find(id).isPresent()).toList());
                assertThat(enabled.findProductIds(LongStream.rangeClosed(1,1001).boxed().toList())).containsExactly(1001L);
            }
            assertThat(enabled.findProductIds(List.of())).isEmpty();
            assertThat(new LocalOfferService(offerJdbc,false).findProductIds(ids)).isEmpty();
        } finally {
            source.destroy();
        }
    }

    @Test
    void repositoryChunksLargeRecallAndDoesNotIssueEmptyInQueries() {
        ProductMapper mapper=mock(ProductMapper.class);
        ProductRepository batched=new ProductRepository(mapper);
        List<Long> ids=LongStream.rangeClosed(1,501).boxed().toList();
        batched.findByIds(ids);batched.findCommerceFactsByIds(ids);
        verify(mapper).findByIds(ids.subList(0,500));
        verify(mapper).findByIds(ids.subList(500,501));
        verify(mapper).findCommerceFactsByIds(ids.subList(0,500));
        verify(mapper).findCommerceFactsByIds(ids.subList(500,501));
        verifyNoMoreInteractions(mapper);
    }
}
