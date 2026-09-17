package com.example.locallife.product;

import org.springframework.stereotype.Repository;

import java.util.List;
import java.util.ArrayList;
import java.util.Optional;

@Repository
public class ProductRepository {
    private final ProductMapper mapper;
    private com.example.locallife.inventory.InventoryReadPort remoteInventory;

    @org.springframework.beans.factory.annotation.Autowired(required=false)
    public void setRemoteInventory(com.example.locallife.inventory.InventoryReadPort client){this.remoteInventory=client;}

    public ProductRepository(ProductMapper mapper) {
        this.mapper = mapper;
    }

    public List<Product> findByFilters(
            String query,
            String category,
            String brand,
            Long minPriceMinor,
            Long maxPriceMinor,
            int limit
    ) {
        return mapper.findByFilters(query, category, brand, minPriceMinor, maxPriceMinor, limit);
    }

    public Optional<Product> findById(Long id) {
        return Optional.ofNullable(mapper.findById(id));
    }

    public List<Product> findByCatalogFilters(String version,String query,String category,String brand,
            Long minPriceMinor,Long maxPriceMinor,int limit) {
        return mapper.findByCatalogFilters(version,query,category,brand,minPriceMinor,maxPriceMinor,limit);
    }

    public List<Product> findByIds(List<Long> ids) {
        List<Long> unique = ids.stream().distinct().toList();
        List<Product> result = new ArrayList<>();
        for (int start = 0; start < unique.size(); start += 500) {
            result.addAll(mapper.findByIds(unique.subList(start, Math.min(start + 500, unique.size()))));
        }
        return result;
    }

    public List<ProductCommerceFacts> findCommerceFactsByIds(List<Long> ids) {
        List<Long> unique = ids.stream().distinct().toList();
        List<ProductCommerceFacts> result = new ArrayList<>();
        for (int start = 0; start < unique.size(); start += 500) {
            var batch=unique.subList(start, Math.min(start + 500, unique.size()));
            if(remoteInventory==null)result.addAll(mapper.findCommerceFactsByIds(batch));
            else {
                var stock=remoteInventory.stocks("PRODUCT",batch).stream().collect(java.util.stream.Collectors.toMap(com.example.locallife.inventory.InventoryStock::itemId,s->s));
                for(var product:mapper.findByIds(batch)){
                    var inventory=stock.get(product.id());
                    result.add(new ProductCommerceFacts(product.id(),product.snapshotPriceMinor(),product.priceStatus(),product.lifecycleStatus(),product.entityVersion(),
                        inventory==null?null:inventory.availableQuantity(),inventory==null?null:inventory.version()));
                }
            }
        }
        return result;
    }

    public int create(CreateProductRequest request) {
        return mapper.insert(request);
    }

    public List<ProductAttribute> findAttributes(Long productId) {
        return mapper.findAttributes(productId);
    }

    public List<Product> findByLifecycleStatus(String status, int limit) {
        return mapper.findByLifecycleStatus(status, limit);
    }

    public List<Product> findByIdAfter(long afterId, int limit) {
        return mapper.findByIdAfter(afterId, limit);
    }

    public List<Product> findPublishedByIdAfter(String version,long afterId,int limit) {
        return mapper.findPublishedByIdAfter(version,afterId,limit);
    }

    public Optional<ProductCommerceFacts> findCommerceFacts(Long productId) {
        if(remoteInventory!=null)return findCommerceFactsByIds(List.of(productId)).stream().findFirst();
        return Optional.ofNullable(mapper.findCommerceFacts(productId));
    }

    public int update(Long productId, ProductUpdateRequest request) {
        return mapper.update(productId, request);
    }

    public int softDelete(Long productId, Long expectedVersion) {
        return mapper.softDelete(productId, expectedVersion);
    }
}
