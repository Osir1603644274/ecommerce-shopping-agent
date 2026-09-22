package com.example.locallife.support;

import com.example.locallife.common.*;
import com.example.locallife.ordering.MoneyAllocation;
import com.example.locallife.integration.OutboxService;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;
import java.sql.Timestamp;
import java.time.Instant;
import java.util.*;
import static com.example.locallife.support.AfterSaleLifecycle.*;

/** Applies verified receipts under the shared order lock. Provider persistence is a separate transaction. */
@Service
public class SupportOperations {
    private final JdbcTemplate jdbc;private final ObjectMapper json;private final SupportService cases;
    private final SupportReceiptSimulator receipts;private final OutboxService outbox;private final boolean enabled;
    private final com.example.locallife.inventory.ReplacementInventory replacementInventory;
    public SupportOperations(JdbcTemplate jdbc,ObjectMapper json,SupportService cases,SupportReceiptSimulator receipts,
            OutboxService outbox,com.example.locallife.inventory.ReplacementInventory replacementInventory,@Value("${local-life.support.enabled:false}") boolean enabled) {
        this.jdbc=jdbc;this.json=json;this.cases=cases;this.receipts=receipts;this.outbox=outbox;this.enabled=enabled;
        this.replacementInventory=replacementInventory;
    }

    @Transactional(isolation=org.springframework.transaction.annotation.Isolation.READ_COMMITTED)
    public SupportService.CaseView chooseWait(String id,String user,long version,String key) {
        requireEnabled();requireKey(key);var current=lock(id,user);
        String digest=SupportReceiptSimulator.hash("CHOOSE_WAIT\n"+version);
        if(replayed(id,key,digest)) return current;
        if(current.version()!=version) throw conflict("售后版本已变化");
        transition(current,AfterSaleLifecycle.apply(state(current),Event.CHOOSE_WAIT),Event.CHOOSE_WAIT,key,digest,user,"{}");
        jdbc.update("UPDATE support_case SET recovery_attempts=0,recovery_error=NULL,recovery_next_at=NULL WHERE id=?",id);
        return cases.get(id,user);
    }

    public record ConversionPreview(String previewId,String caseId,long amountMinor,String currency,Instant expiresAt) { }
    @Transactional(isolation=org.springframework.transaction.annotation.Isolation.READ_COMMITTED)
    public ConversionPreview previewConversion(String id,String user) {
        requireEnabled();var current=lock(id,user);
        requireNoReplacementInFlight(id);
        AfterSaleLifecycle.apply(state(current),Event.CONFIRM_CONVERT_TO_REFUND);
        String preview=UUID.randomUUID().toString();Instant expiry=cases.businessNow(current.orderId()).plusSeconds(300);
        jdbc.update("UPDATE support_conversion_preview SET expires_at=? WHERE case_id=? AND confirmed_key IS NULL",Timestamp.from(cases.businessNow(current.orderId())),id);
        jdbc.update("INSERT INTO support_conversion_preview(id,case_id,user_id,expected_version,amount_minor,expires_at) VALUES(?,?,?,?,?,?)",
                preview,id,user,current.version(),current.amountMinor(),Timestamp.from(expiry));
        return new ConversionPreview(preview,id,current.amountMinor(),current.currency(),expiry);
    }
    @Transactional(isolation=org.springframework.transaction.annotation.Isolation.READ_COMMITTED)
    public SupportService.CaseView confirmConversion(String previewId,String user,String key) {
        requireEnabled();requireKey(key);
        var rows=jdbc.queryForList("SELECT case_id FROM support_conversion_preview WHERE id=? AND user_id=?",previewId,user);
        if(rows.isEmpty()) throw new ResourceNotFoundException("变更确认卡不存在");
        String id=(String)rows.get(0).get("case_id");var current=lock(id,user);
        var preview=jdbc.queryForMap("SELECT * FROM support_conversion_preview WHERE id=?",previewId);
        String digest=SupportReceiptSimulator.hash("CONFIRM_CONVERT_TO_REFUND\n"+previewId);
        if(replayed(id,key,digest)) return current;
        if(preview.get("confirmed_key")!=null) throw conflict("变更确认卡已使用");
        requireNoReplacementInFlight(id);
        if(!((Timestamp)preview.get("expires_at")).toInstant().isAfter(cases.businessNow(current.orderId()))) throw conflict("变更确认卡已过期");
        if(current.version()!=((Number)preview.get("expected_version")).longValue()
                ||current.amountMinor()!=((Number)preview.get("amount_minor")).longValue()) throw conflict("售后状态已改变，请重新预览");
        State next=AfterSaleLifecycle.apply(state(current),Event.CONFIRM_CONVERT_TO_REFUND);
        createRefundCommand(current);
        transition(current,next,Event.CONFIRM_CONVERT_TO_REFUND,key,digest,user,encode(Map.of("previewId",previewId)));
        jdbc.update("UPDATE support_conversion_preview SET confirmed_key=? WHERE id=?",key,previewId);
        return cases.get(id,user);
    }

    @Transactional(isolation=org.springframework.transaction.annotation.Isolation.READ_COMMITTED)
    public SupportService.CaseView submitReturn(String id,String user,long version,String tracking,String key) {
        requireEnabled();requireKey(key);
        if(tracking==null || !tracking.matches("[A-Za-z0-9_-]{3,128}")) throw new InvalidBusinessStateException("退货单号格式无效");
        var current=lock(id,user);String digest=SupportReceiptSimulator.hash("SUBMIT_RETURN\n"+version+"\n"+tracking);
        if(replayed(id,key,digest)) return current;
        if(current.version()!=version) throw conflict("售后版本已变化");
        State next=AfterSaleLifecycle.apply(state(current),Event.SUBMIT_RETURN);
        jdbc.update("INSERT INTO support_return(case_id,tracking_no,item_id,requested_quantity) VALUES(?,?,?,?)",
                id,tracking,current.itemId(),current.quantity());
        transition(current,next,Event.SUBMIT_RETURN,key,digest,user,encode(Map.of("trackingNo",tracking)));
        return cases.get(id,user);
    }

    @Transactional(isolation=org.springframework.transaction.annotation.Isolation.READ_COMMITTED)
    public SupportService.CaseView reconcile(String receiptId,String user) {
        var receipt=receipts.get(receiptId);var current=lock(receipt.caseId(),user);
        receipt=receipts.get(receiptId);
        if("APPLIED".equals(receipt.status())) return current;
        String persistedHash=jdbc.queryForObject("SELECT request_hash FROM support_receipt WHERE id=?",String.class,receiptId);
        if(!SupportReceiptSimulator.hash(receipt.event()+"\n"+receipt.expectedVersion()+"\n"+receipt.payload()).equals(persistedHash))
            throw conflict("回执内容摘要不匹配，需核实");
        if(receipt.expectedVersion()!=current.version()) throw conflict("回执版本与售后状态不一致，需核实");
        Event event=Event.valueOf(receipt.event());JsonNode payload=decode(receipt.payload());
        if((event==Event.RETURN_RECEIVED || event==Event.INSPECTION_ACCEPTED) && !matchingReturn(current,payload))
            event=Event.RECEIPT_MISMATCH;
        State next=AfterSaleLifecycle.apply(state(current),event);
        if(event==Event.REPLACEMENT_DISPATCH_CONFIRMED || event==Event.REPLACEMENT_RECEIPT_CONFIRMED) {
            var rows=jdbc.queryForList("SELECT * FROM support_replacement WHERE id=? AND case_id=?",payload.path("replacementId").asText(),current.id());
            if(rows.size()!=1 || !matchingReturn(current,payload) || !current.specification().equals(payload.path("specification").asText())
                    ||!payload.path("chargeMinor").isIntegralNumber() ||payload.path("chargeMinor").longValue()!=0
                    ||!payload.path("trackingNo").asText().matches("[A-Za-z0-9_-]{3,128}")) throw conflict("补发回执身份或数量规格不匹配");
            var replacement=rows.get(0);String rid=(String)replacement.get("id");
            if(event==Event.REPLACEMENT_DISPATCH_CONFIRMED) {
                if(!"RESERVED".equals(replacement.get("status"))) throw conflict("补发未预占库存");
                // A pending remote command must COMMIT, while the receipt stays pending for reconciliation.
                if(!replacementInventory.dispatch(rid)) return current;
                jdbc.update("UPDATE support_replacement SET status='SHIPPED',tracking_no=?,dispatch_receipt_id=? WHERE id=?",
                        payload.path("trackingNo").asText(),receiptId,rid);
            } else {
                if(!"SHIPPED".equals(replacement.get("status")) || !payload.path("trackingNo").asText().equals(replacement.get("tracking_no")))
                    throw conflict("补发签收缺少匹配出库回执");
                jdbc.update("UPDATE support_replacement SET status='RECEIVED',received_receipt_id=? WHERE id=?",receiptId,rid);
            }
        }
        if(event==Event.RETURN_RECEIVED) {
            if(jdbc.update("UPDATE support_return SET received_quantity=?,receipt_id=? WHERE case_id=? AND receipt_id IS NULL",
                    current.quantity(),receiptId,current.id())!=1) throw conflict("退货收货记录不一致");
        }
        if(event==Event.INSPECTION_ACCEPTED) {
            if(!payload.path("sellable").isBoolean()) throw conflict("验收必须明确可售或隔离");
            if(jdbc.update("UPDATE support_return SET sellable=?,inspection_receipt_id=? WHERE case_id=? AND received_quantity=? AND receipt_id IS NOT NULL AND inspection_receipt_id IS NULL",
                    payload.path("sellable").booleanValue(),receiptId,current.id(),current.quantity())!=1)
                throw conflict("缺少匹配的仓库收货记录");
            jdbc.update("""
                INSERT INTO support_stock_effect(effect_id,case_id,order_id,item_id,quantity,kind,created_at)
                VALUES(?,?,?,?,?,?,?)
                ""","return:"+current.id(),current.id(),current.orderId(),current.itemId(),current.quantity(),
                    payload.path("sellable").booleanValue()?"RETURN_SELLABLE":"RETURN_QUARANTINE",Timestamp.from(Instant.now()));
        }
        if(next.phase()==Phase.REFUND_PENDING && current.phase()!=Phase.REFUND_PENDING) createRefundCommand(current);
        if(event==Event.REFUND_RECEIPT_CONFIRMED) settleRefund(current,receiptId,payload);
        String actor=jdbc.queryForObject("SELECT actor FROM support_receipt WHERE id=?",String.class,receiptId);
        transition(current,next,event,"receipt:"+receiptId,SupportReceiptSimulator.hash(receipt.payload()),actor,
                encode(Map.of("receiptId",receiptId,"payload",payload)));
        jdbc.update("UPDATE support_receipt SET status='APPLIED',applied_at=? WHERE id=?",Timestamp.from(Instant.now()),receiptId);
        return cases.get(current.id(),user);
    }

    @Transactional(isolation=org.springframework.transaction.annotation.Isolation.READ_COMMITTED)
    public SupportService.CaseView reserveReplacement(String id,String user) {
        var current=lock(id,user);
        if(current.phase()!=Phase.WAITING_STOCK || current.type()!=AfterSalePolicy.Type.EXCHANGE) return current;
        if(jdbc.queryForObject("SELECT COUNT(*) FROM support_stock_effect WHERE case_id=? AND status='ACK' AND kind IN ('RETURN_SELLABLE','RETURN_QUARANTINE')",Integer.class,id)!=1)
            return current;
        var attempts=jdbc.queryForList("SELECT * FROM support_replacement WHERE case_id=? AND case_version=?",id,current.version());
        if(attempts.isEmpty()) {
            String replacement=UUID.randomUUID().toString();
            jdbc.update("INSERT INTO support_replacement(id,case_id,case_version,item_id,quantity,specification,reserve_until,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    replacement,id,current.version(),current.itemId(),current.quantity(),current.specification(),Timestamp.from(cases.businessNow(current.orderId()).plusSeconds(2592000)),Timestamp.from(Instant.now()));
            attempts=jdbc.queryForList("SELECT * FROM support_replacement WHERE id=?",replacement);
        }
        var attempt=attempts.get(0);String replacement=(String)attempt.get("id");
        var result=replacementInventory.reserve(replacement,current.itemId(),current.quantity(),java.time.LocalDateTime.ofInstant(((Timestamp)attempt.get("reserve_until")).toInstant(),java.time.ZoneOffset.UTC));
        jdbc.update("UPDATE support_replacement SET status=? WHERE id=?",result.name(),replacement);
        Event event=switch(result) {
            case RESERVED -> Event.STOCK_RESERVED;
            case SHORTAGE -> Event.STOCK_UNAVAILABLE;
            default -> null;
        };
        if(event!=null) transition(current,AfterSaleLifecycle.apply(state(current),event),event,"stock:"+replacement,
                SupportReceiptSimulator.hash(replacement+"\n"+result),"inventory",encode(Map.of("replacementId",replacement,"result",result.name())));
        if(event==Event.STOCK_RESERVED) jdbc.update("UPDATE support_case SET recovery_attempts=0,recovery_error=NULL,recovery_next_at=NULL WHERE id=?",id);
        return cases.get(id,user);
    }
    @Transactional(isolation=org.springframework.transaction.annotation.Isolation.READ_COMMITTED)
    public SupportService.CaseView expireReplacement(String id,String user) {
        requireEnabled();var current=lock(id,user);
        if(current.type()!=AfterSalePolicy.Type.EXCHANGE || !Set.of(Phase.REPLACEMENT_READY,Phase.REPLACEMENT_RELEASING).contains(current.phase())) return current;
        var rows=jdbc.queryForList("SELECT id,status,reserve_until FROM support_replacement WHERE case_id=? AND status IN ('RESERVED','RELEASING','RELEASE_REVIEW')",id);
        if(rows.size()!=1) throw conflict("缺少唯一可核对的补发预占");
        var row=rows.get(0);String replacement=(String)row.get("id");
        if(current.phase()==Phase.REPLACEMENT_READY) {
            if(((Timestamp)row.get("reserve_until")).toInstant().isAfter(cases.businessNow(current.orderId()))) return current;
            // An independently committed dispatch receipt is evidence of a possible physical effect, even before local ACK.
            if(jdbc.queryForObject("SELECT COUNT(*) FROM support_receipt WHERE case_id=? AND event_type='REPLACEMENT_DISPATCH_CONFIRMED'",Integer.class,id)>0) return current;
            transition(current,AfterSaleLifecycle.apply(state(current),Event.BEGIN_REPLACEMENT_RELEASE),Event.BEGIN_REPLACEMENT_RELEASE,
                    "release-start:"+replacement,SupportReceiptSimulator.hash(replacement+"\nrelease"),"recovery",encode(Map.of("replacementId",replacement)));
            jdbc.update("UPDATE support_replacement SET status='RELEASING' WHERE id=?",replacement);
            current=cases.get(id,user);
        }
        var released=replacementInventory.releaseExpired(replacement);
        jdbc.update("UPDATE support_replacement SET status=? WHERE id=?",switch(released) {
            case RELEASED -> "EXPIRED";case REVIEW -> "RELEASE_REVIEW";default -> "RELEASING";
        },replacement);
        if(released==com.example.locallife.inventory.ReplacementInventory.ReleaseResult.RELEASED)
            transition(current,AfterSaleLifecycle.apply(state(current),Event.REPLACEMENT_RELEASED),Event.REPLACEMENT_RELEASED,
                    "release-done:"+replacement,SupportReceiptSimulator.hash(replacement+"\nreleased"),"inventory",encode(Map.of("replacementId",replacement)));
        return cases.get(id,user);
    }
    private void requireNoReplacementInFlight(String id) {
        if(jdbc.queryForObject("SELECT COUNT(*) FROM support_replacement WHERE case_id=? AND status NOT IN ('SHORTAGE','EXPIRED')",Integer.class,id)>0)
            throw conflict("换货库存已预占或结果待核实，不能改为退款");
    }

    /** Explicit administrator decision resumes checking; it never substitutes for fresh warehouse evidence. */
    @Transactional(isolation=org.springframework.transaction.annotation.Isolation.READ_COMMITTED)
    public SupportService.CaseView resumeReview(String id,String user,long version,String ticketId,String actor,String key) {
        requireEnabled();requireKey(key);var current=lock(id,user);
        if(ticketId==null || ticketId.isBlank()) throw conflict("必须关联已经核实的工单");
        String hash=SupportReceiptSimulator.hash("RESUME_REVIEW\n"+version+"\n"+ticketId);
        if(replayed(id,key,hash)) return current;
        if(current.version()!=version || current.phase()!=Phase.NEEDS_REVIEW) throw conflict("售后核实状态或版本已变化");
        if(jdbc.queryForObject("SELECT COUNT(*) FROM support_ticket WHERE id=? AND case_id=? AND order_id=? AND user_id=? AND status IN ('RESOLVED','CLOSED') AND category IN ('AFTERSALE_DISPUTE','INFO_VERIFY')",
                Integer.class,ticketId,id,current.orderId(),user)!=1) throw conflict("关联工单尚未核实或不属于此售后");
        var origins=jdbc.query("SELECT from_phase FROM support_event WHERE case_id=? AND to_phase='NEEDS_REVIEW' ORDER BY created_at DESC,id DESC LIMIT 1",(rs,n)->rs.getString(1),id);
        if(origins.isEmpty()) throw conflict("缺少进入核实阶段的事件证据");
        Event event=switch(origins.get(0)) {
            case "RETURN_IN_TRANSIT" -> Event.RESUME_RETURN_CHECK;
            case "AWAITING_INSPECTION" -> Event.RESUME_INSPECTION;
            default -> throw conflict("此核实原因不允许自动恢复仓库检查");
        };
        if(jdbc.queryForObject("SELECT COUNT(*) FROM support_stock_effect WHERE case_id=?",Integer.class,id)>0
                ||jdbc.queryForObject("SELECT COUNT(*) FROM support_refund_command WHERE case_id=?",Integer.class,id)>0)
            throw conflict("已经产生资金或库存指令，不能回退仓库检查");
        transition(current,AfterSaleLifecycle.apply(state(current),event),event,key,hash,actor,encode(Map.of("ticketId",ticketId,"decision","RESUME_CHECK_ONLY")));
        return cases.get(id,user);
    }

    private void settleRefund(SupportService.CaseView current,String receiptId,JsonNode payload) {
        var commands=jdbc.queryForList("SELECT * FROM support_refund_command WHERE case_id=?",current.id());
        if(commands.size()!=1) throw conflict("退款命令不存在");
        var command=commands.get(0);
        if(!Objects.equals(command.get("payment_id"),payload.path("paymentId").asText())
                ||!Objects.equals(command.get("request_hash"),payload.path("commandHash").asText())
                ||!Objects.equals(command.get("currency"),payload.path("currency").asText())
                ||!payload.path("amountMinor").isIntegralNumber()
                ||((Number)command.get("amount_minor")).longValue()!=payload.path("amountMinor").longValue()
                ||current.amountMinor()!=payload.path("amountMinor").longValue()
                ||!payload.path("providerReference").asText().equals("SIM-SUPPORT-"+current.id()))
            throw conflict("退款回执金额或身份不匹配");
        if(current.type()==AfterSalePolicy.Type.RETURN_REFUND
                &&jdbc.queryForObject("SELECT COUNT(*) FROM support_stock_effect WHERE case_id=? AND kind IN ('RETURN_SELLABLE','RETURN_QUARANTINE') AND status='ACK'",Integer.class,current.id())!=1)
            throw conflict("退货库存处置尚未确认，请继续回查");
        var line=jdbc.queryForMap("SELECT quantity,paid_minor,refunded_quantity,refunded_minor FROM order_line_allocation WHERE order_id=? AND item_id=?",
                current.orderId(),current.itemId());
        int exchanged=jdbc.queryForObject("SELECT COALESCE(SUM(quantity),0) FROM support_case WHERE order_id=? AND item_id=? AND current_type='EXCHANGE' AND phase='COMPLETED'",
                Integer.class,current.orderId(),current.itemId());
        long expected=MoneyAllocation.refund(((Number)line.get("paid_minor")).longValue(),((Number)line.get("quantity")).intValue(),
                Math.addExact(((Number)line.get("refunded_quantity")).intValue(),exchanged),current.quantity());
        if(expected!=current.amountMinor()) throw conflict("退款分摊已改变");
        long already=jdbc.queryForObject("SELECT COALESCE(SUM(refunded_minor),0) FROM order_line_allocation WHERE order_id=?",Long.class,current.orderId());
        Long paid=jdbc.queryForObject("SELECT amount_minor FROM payment_record WHERE id=? AND order_id=? AND status='SUCCESS'",Long.class,
                command.get("payment_id"),current.orderId());
        if(current.amountMinor()>paid-already) throw conflict("退款超过成功支付余额");
        if(jdbc.update("""
            UPDATE order_line_allocation SET refunded_quantity=refunded_quantity+?,refunded_minor=refunded_minor+?
            WHERE order_id=? AND item_id=? AND refunded_quantity+?<=quantity AND refunded_minor+?<=paid_minor
            """,current.quantity(),current.amountMinor(),current.orderId(),current.itemId(),current.quantity(),current.amountMinor())!=1)
            throw conflict("退款数量或金额超限");
        jdbc.update("UPDATE support_refund_command SET status='SUCCESS',provider_receipt_id=?,completed_at=? WHERE case_id=?",
                receiptId,Timestamp.from(Instant.now()),current.id());
        // This receipt never restores unshipped inventory or rewinds historical fulfillment.
        outbox.append("ORDER",current.orderId(),SupportAuditEventHandler.REFUND_COMPLETED,Map.of("orderId",current.orderId(),
                "caseId",current.id(),"receiptId",receiptId,"amountMinor",current.amountMinor(),"currency",current.currency()));
    }
    private void createRefundCommand(SupportService.CaseView current) {
        var payment=jdbc.queryForObject("SELECT id FROM payment_record WHERE order_id=? AND status='SUCCESS'",String.class,current.orderId());
        String hash=SupportReceiptSimulator.hash(current.id()+"\n"+payment+"\n"+current.amountMinor()+"\n"+current.currency());
        jdbc.update("INSERT INTO support_refund_command(case_id,payment_id,request_hash,amount_minor,currency,created_at) VALUES(?,?,?,?,?,?)",
                current.id(),payment,hash,current.amountMinor(),current.currency(),Timestamp.from(Instant.now()));
    }
    private boolean matchingReturn(SupportService.CaseView current,JsonNode payload) {
        return payload.path("itemId").isIntegralNumber() && payload.path("itemId").longValue()==current.itemId()
                &&payload.path("quantity").isIntegralNumber() && payload.path("quantity").longValue()==current.quantity();
    }
    private SupportService.CaseView lock(String id,String user) {
        var first=cases.get(id,user);
        jdbc.query("SELECT id FROM customer_order WHERE id=? FOR UPDATE",(rs,n)->rs.getString(1),first.orderId());
        return cases.get(id,user);
    }
    private boolean replayed(String id,String key,String hash) {
        var old=jdbc.query("SELECT request_hash FROM support_event WHERE case_id=? AND event_key=?",(rs,n)->rs.getString(1),id,key);
        if(old.isEmpty()) return false;
        if(!hash.equals(old.get(0))) throw conflict("操作幂等键冲突");return true;
    }
    private void transition(SupportService.CaseView current,State next,Event event,String key,String hash,String actor,String evidence) {
        Timestamp now=Timestamp.from(Instant.now());
        if(jdbc.update("UPDATE support_case SET phase=?,current_type=?,version=version+1,updated_at=? WHERE id=? AND version=?",
                next.phase().name(),next.type().name(),now,current.id(),current.version())!=1) throw conflict("售后版本冲突");
        if(next.terminal()) jdbc.update("DELETE FROM support_order_claim WHERE case_id=?",current.id());
        jdbc.update("UPDATE customer_order SET version=version+1 WHERE id=?",current.orderId());
        jdbc.update("""
            INSERT INTO support_event(id,case_id,event_key,request_hash,actor,event_type,from_phase,to_phase,evidence_json,created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)
            """,UUID.randomUUID().toString(),current.id(),key,hash,actor,event.name(),current.phase().name(),next.phase().name(),evidence,now);
    }
    private State state(SupportService.CaseView current) { return new State(current.type(),current.phase()); }
    private void requireEnabled() { if(!enabled) throw new ForbiddenOperationException("客服售后写入未启用"); }
    private void requireKey(String key) { if(key==null || key.isBlank() || key.length()>128) throw new InvalidBusinessStateException("操作幂等键无效"); }
    private JsonNode decode(String text) { try{return json.readTree(text);}catch(Exception ex){throw new IllegalStateException("回执损坏",ex);} }
    private String encode(Object value) { try{return json.writeValueAsString(value);}catch(Exception ex){throw new IllegalStateException(ex);} }
    private BusinessConflictException conflict(String message) { return new BusinessConflictException(message); }
}
