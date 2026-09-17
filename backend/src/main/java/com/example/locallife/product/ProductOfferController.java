package com.example.locallife.product;

import com.example.locallife.common.ApiResponse;
import com.example.locallife.common.ResourceNotFoundException;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.web.bind.annotation.*;

@RestController
public class ProductOfferController {
    private final ProductRepository products;
    private final LocalOfferService offers;
    private final JdbcTemplate jdbc;
    private com.example.locallife.inventory.InventoryReadPort remoteInventory;
    @org.springframework.beans.factory.annotation.Autowired(required=false)
    public void setRemoteInventory(com.example.locallife.inventory.InventoryReadPort client){this.remoteInventory=client;}
    public ProductOfferController(ProductRepository products,LocalOfferService offers,JdbcTemplate jdbc) {
        this.products=products;this.offers=offers;this.jdbc=jdbc;
    }
    @GetMapping("/api/products/{id}/offer")
    public ApiResponse<View> get(@PathVariable long id) {
        var p=products.findById(id).orElseThrow(()->new ResourceNotFoundException("商品不存在"));
        var offer=offers.find(id).orElse(null);
        // Keep absent prices nullable: a primitive-long ternary would unbox null.
        Long price=null;
        if (offer!=null) price=offer.priceMinor();
        else if ("verified".equals(p.priceStatus())) price=p.snapshotPriceMinor();
        String kind=offer!=null?offer.kind():p.priceStatus();
        Stock stock;
        if(remoteInventory==null)stock=CatalogReadBudget.query(jdbc,"SELECT s.available_quantity,i.product_id AS imported_id FROM inventory_stock s LEFT JOIN external_catalog_identity i ON i.product_id=s.item_id WHERE s.item_type='PRODUCT' AND s.item_id=?",
            (rs,n)->new Stock(rs.getInt("available_quantity"),rs.getObject("imported_id")!=null),id).stream().findFirst().orElse(null);
        else {
            var rows=remoteInventory.stocks("PRODUCT",java.util.List.of(id));
            boolean imported=!CatalogReadBudget.query(jdbc,"SELECT product_id FROM external_catalog_identity WHERE product_id=?",(rs,n)->rs.getLong(1),id).isEmpty();
            stock=rows.isEmpty()?null:new Stock(rows.get(0).availableQuantity(),imported);
        }
        Integer available=stock==null?null:stock.available();
        boolean active="ACTIVE".equals(p.lifecycleStatus());
        return ApiResponse.ok(new View(price,offer!=null?offer.currency():p.currency(),kind,available,
            active && price!=null && price>0 && available!=null && available>0,p.lifecycleStatus(),stock!=null && stock.imported()));
    }
    private record Stock(Integer available,boolean imported) { }
    public record View(Long priceMinor,String currency,String kind,Integer available,boolean canPurchase,String lifecycleStatus,boolean externalCatalogEligible) {
        public View(Long priceMinor,String currency,String kind,Integer available,boolean canPurchase,String lifecycleStatus) {
            this(priceMinor,currency,kind,available,canPurchase,lifecycleStatus,false);
        }
    }
}
