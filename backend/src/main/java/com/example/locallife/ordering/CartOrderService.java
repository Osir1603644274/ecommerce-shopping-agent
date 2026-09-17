package com.example.locallife.ordering;

import com.example.locallife.common.*;
import com.example.locallife.inventory.InventoryService;
import com.example.locallife.fulfillment.*;
import com.example.locallife.integration.*;
import com.example.locallife.marketing.CouponService;
import org.springframework.dao.DuplicateKeyException;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.time.LocalDateTime;
import java.util.*;

@Service
public class CartOrderService {
    private final OrderMapper orders;
    private final OrderService views;
    private final CommerceCatalogPort catalog;
    private final InventoryService inventory;
    private final CouponService coupons;
    private final OutboxService outbox;
    private final FulfillmentLifecycle fulfillment;
    private final JdbcTemplate jdbc;
    public CartOrderService(OrderMapper orders, OrderService views, CommerceCatalogPort catalog,
            InventoryService inventory, CouponService coupons, OutboxService outbox,
            FulfillmentLifecycle fulfillment, JdbcTemplate jdbc) {
        this.orders=orders; this.views=views; this.catalog=catalog; this.inventory=inventory;
        this.coupons=coupons; this.outbox=outbox; this.fulfillment=fulfillment; this.jdbc=jdbc;
    }

    @Transactional
    public OrderResponse create(CreateCartOrderRequest request, String user, String key) {
        if (key==null || key.isBlank() || key.length()>128) throw new InvalidBusinessStateException("Idempotency-Key 必填且最多128字符");
        if (request==null || request.items()==null || request.items().isEmpty() || request.items().size()>50)
            throw new InvalidBusinessStateException("订单需要1至50种商品");
        var lines=new ArrayList<>(request.items());
        for (var line:lines) if (line==null || !"PRODUCT".equals(line.itemType()) || line.itemId()==null || line.itemId()<=0
                || line.quantity()==null || line.quantity()<=0 || line.quantity()>100000)
            throw new InvalidBusinessStateException("购物车仅支持有效实物商品及数量");
        lines.sort(Comparator.comparing(CreateCartOrderRequest.Line::itemId));
        for(int i=1;i<lines.size();i++) if(lines.get(i-1).itemId().equals(lines.get(i).itemId()))
            throw new InvalidBusinessStateException("同一商品只能出现一次，请合并数量");
        String coupon=request.userCouponId()==null || request.userCouponId().isBlank()?null:request.userCouponId().strip();
        String canonical="cart-v2\n"+lines.stream().map(l->l.itemType()+":"+l.itemId()+":"+l.quantity()).reduce((a,b)->a+"\n"+b).orElseThrow()
                +"\ncoupon:"+(coupon==null?"":coupon);
        // Retain legacy hashes when callers have no expected quote; bind all
        // browser-confirmed amounts when present so keys cannot change meaning.
        if(request.expectedPayableMinor()!=null || lines.stream().anyMatch(l->l.expectedUnitPriceMinor()!=null))
            canonical += "\nquote:"+request.expectedPayableMinor()+":"+lines.stream().map(l->String.valueOf(l.expectedUnitPriceMinor())).toList();
        String hash=hash(canonical);
        CustomerOrder existing=orders.findByIdempotencyKey(user,key.strip());
        if(existing!=null) {
            if(!hash.equals(existing.requestHash())) throw new BusinessConflictException("下单幂等键参数冲突");
            return views.get(existing.id(),user,false);
        }
        fulfillment.requireCartSupport();
        var resolved=new ArrayList<Resolved>();
        long total=0;
        try {
            for(var line:lines) {
                var item=catalog.requireItem(line.itemType(),line.itemId());
                if(line.expectedUnitPriceMinor()!=null && line.expectedUnitPriceMinor()!=item.unitPriceMinor())
                    throw new BusinessConflictException("商品价格已变化，请重新预览并确认");
                if(!"CNY".equals(item.currency()) || item.unitPriceMinor()<0) throw new InvalidBusinessStateException("商品币种或价格不合法");
                long subtotal=Math.multiplyExact(item.unitPriceMinor(),line.quantity());
                total=Math.addExact(total,subtotal);
                resolved.add(new Resolved(line,item,inventory.getStock(line.itemType(),line.itemId()).id(),subtotal));
            }
        } catch(ArithmeticException invalid) { throw new InvalidBusinessStateException("订单金额溢出"); }
        String id=UUID.randomUUID().toString();
        LocalDateTime now=LocalDateTime.now(java.time.Clock.systemUTC()), expires=now.plusMinutes(15);
        long discount=coupons.consume(coupon,user,id,total);
        if(request.expectedPayableMinor()!=null && request.expectedPayableMinor()!=total-discount)
            throw new BusinessConflictException("应付金额已变化，请重新预览并确认");
        var allocations=MoneyAllocation.discount(resolved.stream().map(Resolved::subtotal).toList(),discount);
        CustomerOrder order=new CustomerOrder(id,"C"+UUID.randomUUID().toString().replace("-","").substring(0,30),user,key.strip(),hash,
                "PENDING_PAYMENT",total,discount,total-discount,"CNY",coupon,expires,null,null,null,0L,null,null);
        try { orders.insertOrder(order); }
        catch(DuplicateKeyException raced) { throw new BusinessConflictException("相同幂等键的订单正在创建，请重试查询"); }
        // Acquire stock X locks before allocation foreign keys can take shared stock locks.
        // Preserve item-id order: allocations[i] belongs to resolved[i], not to stock-id order.
        for(var r:resolved.stream().sorted(Comparator.comparing(Resolved::stockId)).toList())
            inventory.reserve(id,r.line.itemType(),r.line.itemId(),r.line.quantity(),expires);
        for(int i=0;i<resolved.size();i++) {
            var r=resolved.get(i);
            orders.insertItem(new OrderItem(null,id,r.line.itemType(),r.line.itemId(),r.item.title(),r.item.unitPriceMinor(),
                    r.line.quantity(),r.subtotal,r.item.evidenceJson()));
            jdbc.update("""
                INSERT INTO order_line_allocation(order_id,item_type,item_id,stock_id,quantity,subtotal_minor,discount_minor,paid_minor)
                VALUES(?,?,?,?,?,?,?,?)
                """,id,r.line.itemType(),r.line.itemId(),r.stockId,r.line.quantity(),r.subtotal,allocations.get(i),r.subtotal-allocations.get(i));
        }
        fulfillment.enrollCart(id,lines.stream().map(l->new WarehouseCartCommand.Item(l.itemType(),l.itemId(),l.quantity())).toList());
        outbox.append("ORDER",id,DomainEventTypes.ORDER_CREATED_V1,Map.of("orderId",id,"userId",user,"status","PENDING_PAYMENT","payableMinor",total-discount,"currency","CNY"));
        return views.get(id,user,false);
    }
    private static String hash(String canonical) {
        try { return HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256").digest(canonical.getBytes(StandardCharsets.UTF_8))); }
        catch(java.security.NoSuchAlgorithmException e) { throw new IllegalStateException(e); }
    }
    private record Resolved(CreateCartOrderRequest.Line line,CommerceItemSnapshot item,Long stockId,long subtotal) { }
}
