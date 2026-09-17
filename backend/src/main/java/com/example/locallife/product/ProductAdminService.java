package com.example.locallife.product;

import com.example.locallife.common.BusinessConflictException;
import com.example.locallife.common.InvalidBusinessStateException;
import com.example.locallife.common.ResourceNotFoundException;
import com.example.locallife.integration.ProductSearchEventService;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;
import org.springframework.transaction.support.TransactionSynchronization;
import org.springframework.transaction.support.TransactionSynchronizationManager;

import java.util.Optional;

@Service
public class ProductAdminService {
    private final ProductRepository products;
    private final ProductSearchEventService searchEvents;
    private final Optional<ProductCache> cache;
    private final com.example.locallife.integration.CacheInvalidationRequests invalidation;

    public ProductAdminService(
            ProductRepository products,
            ProductSearchEventService searchEvents,
            Optional<ProductCache> cache
    ) {
        this(products,searchEvents,cache,null);
    }

    @org.springframework.beans.factory.annotation.Autowired
    public ProductAdminService(ProductRepository products, ProductSearchEventService searchEvents,
            Optional<ProductCache> cache, com.example.locallife.integration.CacheInvalidationRequests invalidation) {
        this.products = products;
        this.searchEvents = searchEvents;
        this.cache = cache;
        this.invalidation = invalidation;
    }

    @Transactional
    public ProductMutationReceipt create(CreateProductRequest request) {
        try {
            if (products.create(request) != 1) {
                throw new IllegalStateException("商品新增未写入 MySQL");
            }
        } catch (org.springframework.dao.DuplicateKeyException exception) {
            throw new BusinessConflictException("商品 ID 或来源商品编号已存在");
        }
        Product created = products.findById(request.id())
                .orElseThrow(() -> new IllegalStateException("新增商品后无法读取 MySQL 权威记录"));
        if (!Long.valueOf(1L).equals(created.entityVersion())
                || !"ACTIVE".equalsIgnoreCase(created.lifecycleStatus())) {
            throw new IllegalStateException("新增商品没有从 ACTIVE/v1 开始");
        }
        String eventId = searchEvents.productUpserted(created.id(), created.entityVersion());
        evictAfterCommit(created.id());
        return new ProductMutationReceipt(created.id(), created.entityVersion(), "UPSERT", eventId);
    }

    @Transactional
    public ProductMutationReceipt update(long productId, ProductUpdateRequest request) {
        Product current = requireMutable(productId, request.expectedVersion());
        validatePriceState(current, request);
        if (products.update(productId, request) != 1) {
            throw new BusinessConflictException("商品版本已变化，请刷新后重试");
        }
        Product updated = products.findById(productId)
                .orElseThrow(() -> new IllegalStateException("更新商品后无法读取 MySQL 权威记录"));
        long newVersion = requireVersionAdvance(current, updated);
        String eventId = searchEvents.productUpserted(productId, newVersion);
        evictAfterCommit(productId);
        return new ProductMutationReceipt(productId, newVersion, "UPSERT", eventId);
    }

    @Transactional
    public ProductMutationReceipt delete(long productId, long expectedVersion) {
        Product current = requireMutable(productId, expectedVersion);
        if (products.softDelete(productId, expectedVersion) != 1) {
            throw new BusinessConflictException("商品版本已变化，请刷新后重试");
        }
        Product deleted = products.findById(productId)
                .orElseThrow(() -> new IllegalStateException("删除商品后无法读取 MySQL 权威记录"));
        long newVersion = requireVersionAdvance(current, deleted);
        if (!"DELETED".equalsIgnoreCase(deleted.lifecycleStatus())) {
            throw new IllegalStateException("商品删除状态未持久化");
        }
        String eventId = searchEvents.productDeleted(productId, newVersion);
        evictAfterCommit(productId);
        return new ProductMutationReceipt(productId, newVersion, "DELETE", eventId);
    }

    private Product requireMutable(long productId, long expectedVersion) {
        Product product = products.findById(productId)
                .orElseThrow(() -> new ResourceNotFoundException("商品不存在"));
        if (!"ACTIVE".equalsIgnoreCase(product.lifecycleStatus())) {
            throw new BusinessConflictException("商品已停用或删除");
        }
        if (!Long.valueOf(expectedVersion).equals(product.entityVersion())) {
            throw new BusinessConflictException("商品版本已变化，请刷新后重试");
        }
        return product;
    }

    private static void validatePriceState(Product current, ProductUpdateRequest request) {
        String status = request.priceStatus() == null ? current.priceStatus() : request.priceStatus();
        Long price = request.snapshotPriceMinor() == null
                ? current.snapshotPriceMinor() : request.snapshotPriceMinor();
        String currency = request.currency() == null ? current.currency() : request.currency();
        if ("verified".equals(status) && (price == null || currency == null || currency.isBlank())) {
            throw new InvalidBusinessStateException("已核验价格必须同时包含金额和币种");
        }
    }

    private static long requireVersionAdvance(Product before, Product after) {
        long expected = before.entityVersion() + 1;
        if (after.entityVersion() == null || after.entityVersion() != expected) {
            throw new IllegalStateException("商品版本未按预期递增");
        }
        return expected;
    }

    private void evictAfterCommit(long productId) {
        if (invalidation != null) invalidation.productUpdated(productId);
        if (cache.isEmpty()) {
            return;
        }
        Runnable eviction = () -> cache.get().deleteDetail(productId);
        if (!TransactionSynchronizationManager.isSynchronizationActive()) {
            eviction.run();
            return;
        }
        TransactionSynchronizationManager.registerSynchronization(new TransactionSynchronization() {
            @Override
            public void afterCommit() {
                eviction.run();
            }
        });
    }
}
