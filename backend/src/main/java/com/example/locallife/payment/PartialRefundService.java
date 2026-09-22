package com.example.locallife.payment;

import com.example.locallife.common.*;
import com.example.locallife.fulfillment.*;
import com.example.locallife.inventory.InventoryService;
import com.example.locallife.ordering.*;
import com.example.locallife.integration.OutboxService;
import com.example.locallife.integration.DomainEventTypes;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.*;

@Service
public class PartialRefundService {
    private final JdbcTemplate jdbc;
    private final OrderService orders;
    private final InventoryService inventory;
    private final FulfillmentLifecycle fulfillment;
    private final OutboxService outbox;
    public PartialRefundService(JdbcTemplate jdbc,OrderService orders,InventoryService inventory,
                                FulfillmentLifecycle fulfillment,OutboxService outbox) {
        this.jdbc=jdbc;this.orders=orders;this.inventory=inventory;this.fulfillment=fulfillment;this.outbox=outbox;
    }

    @Transactional
    public PartialRefundView create(String orderId,String user,String key,PartialRefundRequest request) {
        if(key==null || key.isBlank() || key.length()>128) throw new InvalidBusinessStateException("退款 Idempotency-Key 必填且最多128字符");
        if(request==null || request.items()==null || request.items().isEmpty() || request.items().size()>50
                || request.reason()==null || request.reason().isBlank() || request.reason().length()>255)
            throw new InvalidBusinessStateException("退款商品和原因不能为空");
        var requested=new ArrayList<>(request.items());
        for(var l:requested) if(l==null || l.itemId()==null || l.itemId()<=0 || l.quantity()==null || l.quantity()<=0)
            throw new InvalidBusinessStateException("退款商品及数量不合法");
        requested.sort(Comparator.comparing(PartialRefundRequest.Line::itemId));
        for(int i=1;i<requested.size();i++) if(requested.get(i-1).itemId().equals(requested.get(i).itemId()))
            throw new InvalidBusinessStateException("退款商品重复，请合并数量");
        String canonical="quantity-refund-v2\n"+requested.stream().map(l->l.itemId()+":"+l.quantity()).reduce((a,b)->a+"\n"+b).orElseThrow()+"\n"+request.reason().strip();
        String hash=hash(canonical);
        if(request.expectedAmountMinor()!=null) hash=hash(canonical+"\nexpected:"+request.expectedAmountMinor());
        lockOwned(orderId,user);
        var existing=jdbc.query("SELECT id,request_hash FROM partial_refund WHERE order_id=? AND idempotency_key=?",
                (rs,n)->List.of(rs.getString(1),rs.getString(2)),orderId,key.strip());
        if(!existing.isEmpty()) {
            if(!hash.equals(existing.get(0).get(1))) throw new BusinessConflictException("退款幂等键参数冲突");
            return get(existing.get(0).get(0),user);
        }
        CustomerOrder order=orders.requireOwnedOrder(orderId,user);
        if(jdbc.queryForObject("SELECT COUNT(*) FROM support_order_claim WHERE order_id=?",Integer.class,orderId)>0)
            throw new BusinessConflictException("此订单已有售后处理中，请查询原售后单");
        if(!"PAID".equals(order.status())) throw new BusinessConflictException("只有已支付订单支持按数量退款");
        if(jdbc.queryForObject("SELECT COUNT(*) FROM partial_refund WHERE order_id=? AND status='PROCESSING'",Integer.class,orderId)>0)
            throw new BusinessConflictException("此订单已有退款处理中，请先对账");
        if(jdbc.queryForObject("SELECT COUNT(*) FROM refund_record WHERE order_id=?",Integer.class,orderId)>0)
            throw new BusinessConflictException("不能混用整单与按数量退款");
        var payment=jdbc.query("SELECT id,amount_minor,currency FROM payment_record WHERE order_id=? AND status='SUCCESS'",
                (rs,n)->new Paid(rs.getString(1),rs.getLong(2),rs.getString(3)),orderId).stream().findFirst()
                .orElseThrow(()->new BusinessConflictException("无成功支付可供退款"));
        var balances=balances(orderId);
        if(balances.isEmpty()) throw new BusinessConflictException("仅新购物车订单支持按数量退款");
        var selected=new ArrayList<PartialRefundView.Item>();
        long amount=0;
        for(var l:requested) {
            var b=balances.stream().filter(v->v.itemId.equals(l.itemId())).findFirst()
                    .orElseThrow(()->new BusinessConflictException("退款商品不属于订单"));
            if(l.quantity()>b.quantity-b.refundedQuantity) throw new BusinessConflictException("退款数量超过剩余数量");
            long value=MoneyAllocation.refund(b.paidMinor,b.quantity,b.refundedQuantity,l.quantity());
            if(value>b.paidMinor-b.refundedMinor) throw new BusinessConflictException("退款金额超过商品实付余额");
            amount=Math.addExact(amount,value);
            selected.add(new PartialRefundView.Item(l.itemId(),l.quantity(),value));
        }
        long refunded=balances.stream().mapToLong(Balance::refundedMinor).sum();
        if(request.expectedAmountMinor()!=null && request.expectedAmountMinor()!=amount)
            throw new BusinessConflictException("可退金额已变化，请重新预览并确认");
        if(amount>payment.amount-refunded) throw new BusinessConflictException("累计退款超过成功支付金额");
        fulfillment.holdCartRefund(orderId);
        String id=UUID.randomUUID().toString();
        jdbc.update("""
            INSERT INTO partial_refund(id,order_id,payment_id,user_id,idempotency_key,request_hash,amount_minor,currency,reason,status)
            VALUES(?,?,?,?,?,?,?,?,?,'PROCESSING')
            """,id,orderId,payment.id,user,key.strip(),hash,amount,payment.currency,request.reason().strip());
        for(var item:selected) jdbc.update("INSERT INTO partial_refund_item(refund_id,item_id,quantity,amount_minor) VALUES(?,?,?,?)",
                id,item.itemId(),item.quantity(),item.amountMinor());
        return get(id,user);
    }

    @Transactional(readOnly=true)
    public PartialRefundView get(String id,String user) {
        var result=jdbc.query("SELECT id,order_id,user_id,status,amount_minor,currency,provider_refund_no FROM partial_refund WHERE id=?",(rs,n)-> {
            if(!user.equals(rs.getString("user_id"))) throw new ForbiddenOperationException("无权访问退款单");
            return new PartialRefundView(rs.getString("id"),rs.getString("order_id"),rs.getString("status"),rs.getLong("amount_minor"),rs.getString("currency"),rs.getString("provider_refund_no"),List.of());
        },id).stream().findFirst().orElseThrow(()->new ResourceNotFoundException("退款单不存在"));
        var items=jdbc.query("SELECT item_id,quantity,amount_minor FROM partial_refund_item WHERE refund_id=? ORDER BY item_id",
                (rs,n)->new PartialRefundView.Item(rs.getLong(1),rs.getInt(2),rs.getLong(3)),id);
        return new PartialRefundView(result.id(),result.orderId(),result.status(),result.amountMinor(),result.currency(),result.providerRefundNo(),items);
    }

    /** Apply only a durable matching provider receipt; missing evidence keeps PROCESSING and REFUND_HOLD. */
    @Transactional(isolation=org.springframework.transaction.annotation.Isolation.READ_COMMITTED)
    public PartialRefundView reconcile(String id,String user) {
        var initial=get(id,user);
        lockOwned(initial.orderId(),user);
        var refund=get(id,user);
        if("SUCCESS".equals(refund.status())) return refund;
        var receipts=jdbc.query("""
            SELECT p.provider_refund_no,p.amount_minor,p.currency,p.request_hash,p.payment_id,r.request_hash,r.payment_id
            FROM local_refund_receipt p JOIN partial_refund r ON r.id=p.refund_id WHERE p.refund_id=?
            """,(rs,n)->new Receipt(rs.getString(1),rs.getLong(2),rs.getString(3),rs.getString(4),rs.getString(5),rs.getString(6),rs.getString(7)),id);
        if(receipts.isEmpty()) return refund;
        var receipt=receipts.get(0);
        if(receipt.amount!=refund.amountMinor() || !receipt.currency.equals(refund.currency())
                || !receipt.requestHash.equals(receipt.expectedHash) || !receipt.paymentId.equals(receipt.expectedPayment))
            throw new BusinessConflictException("退款渠道回执身份或金额不匹配，需人工对账");
        var current=balances(refund.orderId());
        for(var b:current.stream().sorted(Comparator.comparing(Balance::stockId)).toList()) {
            var item=refund.items().stream().filter(l->l.itemId().equals(b.itemId)).findFirst();
            if(item.isEmpty()) continue;
            var line=item.get();
            if(line.amountMinor()!=MoneyAllocation.refund(b.paidMinor,b.quantity,b.refundedQuantity,line.quantity()))
                throw new BusinessConflictException("退款分摊与行余额不一致");
            if(jdbc.update("""
                UPDATE order_line_allocation SET refunded_quantity=refunded_quantity+?,refunded_minor=refunded_minor+?
                WHERE order_id=? AND item_id=? AND refunded_quantity+?<=quantity AND refunded_minor+?<=paid_minor
                """,line.quantity(),line.amountMinor(),refund.orderId(),line.itemId(),line.quantity(),line.amountMinor())!=1)
                throw new BusinessConflictException("退款数量或金额超限");
            inventory.restoreUnshippedQuantity(refund.orderId(),"PRODUCT",line.itemId(),line.quantity(),refund.id());
        }
        var remaining=balances(refund.orderId()).stream().filter(b->b.quantity>b.refundedQuantity)
                .map(b->new WarehouseCartCommand.Item("PRODUCT",b.itemId,b.quantity-b.refundedQuantity)).toList();
        fulfillment.resumeCartAfterRefund(refund.orderId(),remaining);
        if(jdbc.update("UPDATE partial_refund SET status='SUCCESS',provider_refund_no=?,refunded_at=CURRENT_TIMESTAMP WHERE id=? AND status='PROCESSING'",receipt.number,id)!=1)
            throw new BusinessConflictException("退款状态已改变");
        if(remaining.isEmpty()) {
            if(jdbc.update("UPDATE customer_order SET status='REFUNDED',version=version+1,updated_at=CURRENT_TIMESTAMP WHERE id=? AND status='PAID'",refund.orderId())!=1)
                throw new BusinessConflictException("订单状态已改变");
        }
        outbox.append("ORDER",refund.orderId(),remaining.isEmpty()?DomainEventTypes.ORDER_REFUNDED_V1:DomainEventTypes.ORDER_PARTIAL_REFUNDED_V2,
                Map.of("orderId",refund.orderId(),"refundId",id,"amountMinor",refund.amountMinor(),"currency",refund.currency()));
        return get(id,user);
    }

    @Transactional(readOnly=true)
    public List<Balance> balance(String orderId,String user) { orders.requireOwnedOrder(orderId,user); return balances(orderId); }

    @Transactional(readOnly=true)
    public PartialRefundView byKey(String orderId,String user,String key) {
        orders.requireOwnedOrder(orderId,user);
        String id=jdbc.query("SELECT id FROM partial_refund WHERE order_id=? AND user_id=? AND idempotency_key=?",
            (rs,n)->rs.getString(1),orderId,user,key).stream().findFirst()
            .orElseThrow(()->new ResourceNotFoundException("退款请求尚无回执"));
        return get(id,user);
    }

    @Transactional(readOnly=true)
    public Quote quote(String orderId,String user,PartialRefundRequest request) {
        var order=orders.requireOwnedOrder(orderId,user);
        if(!"PAID".equals(order.status())) throw new BusinessConflictException("只有已支付订单可以申请退款");
        var rows=balance(orderId,user);
        var selected=new ArrayList<PartialRefundView.Item>();
        var seen=new HashSet<Long>(); long amount=0;
        for(var line:request.items()) {
            if(!seen.add(line.itemId())) throw new BusinessConflictException("退款商品不能重复");
            var b=rows.stream().filter(r->r.itemId().equals(line.itemId())).findFirst()
                .orElseThrow(()->new BusinessConflictException("该明细不支持按数量退款"));
            if(line.quantity()<=0 || line.quantity()>b.quantity()-b.refundedQuantity())
                throw new BusinessConflictException("退款数量超过剩余数量");
            long value=MoneyAllocation.refund(b.paidMinor(),b.quantity(),b.refundedQuantity(),line.quantity());
            amount=Math.addExact(amount,value);
            selected.add(new PartialRefundView.Item(line.itemId(),line.quantity(),value));
        }
        return new Quote(orderId,amount,"CNY",selected);
    }
    public record Quote(String orderId,long amountMinor,String currency,List<PartialRefundView.Item> items) { }
    private void lockOwned(String id,String user) {
        var owner=jdbc.query("SELECT user_id FROM customer_order WHERE id=? FOR UPDATE",(rs,n)->rs.getString(1),id).stream().findFirst()
                .orElseThrow(()->new ResourceNotFoundException("订单不存在"));
        if(!owner.equals(user)) throw new ForbiddenOperationException("无权访问订单");
    }
    private List<Balance> balances(String id) {
        return jdbc.query("SELECT item_id,stock_id,quantity,subtotal_minor,discount_minor,paid_minor,refunded_quantity,refunded_minor FROM order_line_allocation WHERE order_id=? ORDER BY item_id",
                (rs,n)->new Balance(rs.getLong(1),rs.getLong(2),rs.getInt(3),rs.getLong(4),rs.getLong(5),rs.getLong(6),rs.getInt(7),rs.getLong(8)),id);
    }
    private static String hash(String s) {
        try { return HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256").digest(s.getBytes(StandardCharsets.UTF_8))); }
        catch(java.security.NoSuchAlgorithmException e) { throw new IllegalStateException(e); }
    }
    public record Balance(Long itemId,Long stockId,int quantity,long subtotalMinor,long discountMinor,long paidMinor,int refundedQuantity,long refundedMinor) { }
    private record Paid(String id,long amount,String currency) { }
    private record Receipt(String number,long amount,String currency,String requestHash,String paymentId,String expectedHash,String expectedPayment) { }
}
