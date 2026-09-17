package com.example.locallife.catalogcontract;

import com.example.locallife.common.InvalidBusinessStateException;
import com.example.locallife.common.ResourceNotFoundException;
import com.example.locallife.ordering.CommerceItemSnapshot;
import com.example.locallife.product.Product;
import com.example.locallife.product.ProductRepository;
import com.example.locallife.shop.Shop;
import com.example.locallife.shop.ShopRepository;
import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.springframework.stereotype.Service;

import java.util.Map;

@Service
public class CommerceCatalogReadService {
    private final ProductRepository products;
    private final ShopRepository shops;
    private final ObjectMapper json;
    private final com.example.locallife.product.LocalOfferService offers;

    public CommerceCatalogReadService(
            ProductRepository products,
            ShopRepository shops,
            ObjectMapper json
    ) {
        this(products, shops, json, null);
    }

    @org.springframework.beans.factory.annotation.Autowired
    public CommerceCatalogReadService(ProductRepository products, ShopRepository shops, ObjectMapper json,
            com.example.locallife.product.LocalOfferService offers) {
        this.products = products;
        this.shops = shops;
        this.json = json;
        this.offers = offers;
    }

    public CommerceItemSnapshot requireItem(String rawItemType, Long itemId) {
        String itemType = rawItemType == null ? "" : rawItemType.strip().toUpperCase();
        if ("PRODUCT".equals(itemType)) {
            return product(itemId);
        }
        if ("LOCAL_DEAL".equals(itemType)) {
            return shop(itemId);
        }
        throw new InvalidBusinessStateException("仅支持 PRODUCT 或 LOCAL_DEAL");
    }

    private CommerceItemSnapshot product(Long itemId) {
        Product product = products.findById(itemId)
                .orElseThrow(() -> new ResourceNotFoundException("商品不存在"));
        if (!"ACTIVE".equals(product.lifecycleStatus())) {
            throw new InvalidBusinessStateException("商品已退出在售目录，不能创建新订单");
        }
        var local = offers == null ? null : offers.find(itemId).orElse(null);
        if (local != null) {
            return new CommerceItemSnapshot("PRODUCT",product.id(),product.title(),local.priceMinor(),
                    local.currency(),local.version(),evidence("product",product.source(),product.provenanceUrl(),
                    "local_simulated",product.datasetRevision()));
        }
        if (product.snapshotPriceMinor() == null
                || !"verified".equalsIgnoreCase(product.priceStatus())) {
            throw new InvalidBusinessStateException("商品缺少已验证快照价格，不能创建订单");
        }
        long version = product.entityVersion() == null ? 0 : product.entityVersion();
        return new CommerceItemSnapshot(
                "PRODUCT", product.id(), product.title(), product.snapshotPriceMinor(),
                product.currency(), version,
                evidence("product", product.source(), product.provenanceUrl(),
                        product.priceStatus(), product.datasetRevision())
        );
    }

    private CommerceItemSnapshot shop(Long itemId) {
        Shop shop = shops.findById(itemId)
                .orElseThrow(() -> new ResourceNotFoundException("本地生活商品不存在"));
        if (shop.avgPrice() == null || shop.avgPrice() < 0) {
            throw new InvalidBusinessStateException("本地生活商品缺少有效价格");
        }
        return new CommerceItemSnapshot(
                "LOCAL_DEAL", shop.id(), shop.name() + " 到店消费",
                Math.multiplyExact(shop.avgPrice().longValue(), 100L), "CNY", 0,
                evidence("local_deal", shop.source(), shop.sourceEntityId(),
                        "shop_avg_price", shop.localizationVersion())
        );
    }

    private String evidence(
            String type,
            String source,
            String provenance,
            String priceStatus,
            String revision
    ) {
        try {
            return json.writeValueAsString(Map.of(
                    "type", safe(type),
                    "source", safe(source),
                    "provenance", safe(provenance),
                    "priceStatus", safe(priceStatus),
                    "revision", safe(revision)
            ));
        } catch (JsonProcessingException exception) {
            throw new IllegalStateException("订单证据快照序列化失败", exception);
        }
    }

    private static String safe(String value) {
        return value == null ? "" : value;
    }
}
