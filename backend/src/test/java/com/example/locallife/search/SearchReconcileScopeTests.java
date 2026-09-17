package com.example.locallife.search;
import com.example.locallife.product.ProductRepository;
import com.example.locallife.shop.ShopRepository;
import io.micrometer.core.instrument.simple.SimpleMeterRegistry;
import org.junit.jupiter.api.Test;
import java.util.List;
import static org.mockito.Mockito.*;

class SearchReconcileScopeTests {
    @Test void scopedDeploymentDoesNotScanWholeTradingCatalog() {
        var products=mock(ProductRepository.class);var shops=mock(ShopRepository.class);
        var gateway=mock(ElasticsearchGateway.class);
        var properties=new SearchProperties(true,null,null,null,null,0,null,null,100);
        var target=new SearchIndexReconciler(gateway,products,shops,properties,new SimpleMeterRegistry());
        target.setReconcileCatalogVersion("frozen");
        when(products.findPublishedByIdAfter("frozen",0,100)).thenReturn(List.of());
        target.reconcile();
        verify(products).findPublishedByIdAfter("frozen",0,100);
        verify(products,never()).findByIdAfter(anyLong(),anyInt());
    }
}
