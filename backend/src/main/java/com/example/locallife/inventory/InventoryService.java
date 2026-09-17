package com.example.locallife.inventory;

import com.example.locallife.common.BusinessConflictException;
import com.example.locallife.common.InvalidBusinessStateException;
import com.example.locallife.common.ResourceNotFoundException;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.time.LocalDateTime;
import java.util.UUID;

@Service
public class InventoryService {
    private final InventoryMapper mapper;
    private com.example.locallife.inventory.remote.DistributedInventory remote;

    @org.springframework.beans.factory.annotation.Autowired(required=false)
    public void setRemote(com.example.locallife.inventory.remote.DistributedInventory remote) { this.remote=remote; }

    public InventoryService(InventoryMapper mapper) {
        this.mapper = mapper;
    }

    @Transactional
    public InventoryReservation reserve(
            String orderId,
            String itemType,
            Long itemId,
            int quantity,
            LocalDateTime expiresAt
    ) {
        if(remote!=null)return remote.reserve(orderId,itemType,itemId,quantity,expiresAt);
        InventoryStock stock = requireStock(itemType, itemId);
        InventoryReservation existing = mapper.findReservation(orderId, stock.id());
        if (existing != null) {
            if (existing.quantity() != quantity) throw new BusinessConflictException("库存预占参数冲突");
            return existing;
        }
        if (mapper.reserve(stock.id(), quantity) != 1) {
            throw new BusinessConflictException("库存不足");
        }
        String reservationId = UUID.randomUUID().toString();
        mapper.insertReservation(reservationId, orderId, stock.id(), quantity, expiresAt);
        return mapper.findReservation(orderId, stock.id());
    }

    @Transactional
    public void confirm(String orderId) {
        if(remote!=null){remote.enqueue(orderId,"CONFIRM");return;}
        for (InventoryReservation reservation : requireReservations(orderId)) {
            if ("CONFIRMED".equals(reservation.status())) continue;
            if (!"RESERVED".equals(reservation.status())
                    || mapper.transitionOne(reservation.id(), "RESERVED", "CONFIRMED") != 1
                    || mapper.confirm(reservation.stockId(), reservation.quantity()) != 1)
                throw new InvalidBusinessStateException("库存预占状态不允许确认");
        }
    }

    @Transactional
    public void release(String orderId, boolean expired) {
        if(remote!=null){remote.enqueue(orderId,"RELEASE");return;}
        for (InventoryReservation reservation : requireReservations(orderId)) {
            if ("RELEASED".equals(reservation.status()) || "EXPIRED".equals(reservation.status())) continue;
            if (!"RESERVED".equals(reservation.status())
                    || mapper.transitionOne(reservation.id(), "RESERVED", expired ? "EXPIRED" : "RELEASED") != 1
                    || mapper.release(reservation.stockId(), reservation.quantity()) != 1)
                throw new InvalidBusinessStateException("库存预占状态不允许释放");
        }
    }

    /** Caller must establish that the paid order was cancelled before external dispatch. */
    @Transactional
    public boolean restoreUnshipped(String orderId) {
        if(remote!=null){remote.enqueue(orderId,"RESTORE_ALL");return true;}
        InventoryReservation reservation = requireReservation(orderId);
        if ("REFUNDED".equals(reservation.status())) return false;
        if (!"CONFIRMED".equals(reservation.status()) || mapper.refundConfirmedReservation(orderId) != 1
                || mapper.restoreSold(reservation.stockId(), reservation.quantity()) != 1)
            throw new InvalidBusinessStateException("未出库退款的库存状态不允许恢复");
        return true;
    }

    public InventoryStock getStock(String itemType, Long itemId) {
        if(remote!=null)return remote.stock(itemType,itemId);
        return requireStock(itemType, itemId);
    }

    /** Only invoke under the order lock after proving that dispatch has never started. */
    @Transactional
    public void restoreUnshippedQuantity(String orderId, String itemType, Long itemId, int quantity) {
        if(remote!=null)throw new IllegalStateException("distributed_refund_requires_refund_id");
        InventoryStock stock = requireStock(itemType, itemId);
        if (quantity <= 0 || mapper.refundQuantity(orderId, stock.id(), quantity) != 1
                || mapper.restoreSold(stock.id(), quantity) != 1)
            throw new InvalidBusinessStateException("退款数量超过已确认且未退的库存");
    }

    @Transactional
    public void restoreUnshippedQuantity(String orderId,String itemType,Long itemId,int quantity,String refundId) {
        if(remote!=null){remote.refund(orderId,refundId,itemType,itemId,quantity);return;}
        restoreUnshippedQuantity(orderId,itemType,itemId,quantity);
    }

    @Transactional
    public InventoryStock createStock(String itemType, Long itemId, int quantity) {
        if(remote!=null)return remote.createStock(itemType,itemId,quantity);
        mapper.insertStock(itemType, itemId, quantity);
        return requireStock(itemType, itemId);
    }

    private InventoryStock requireStock(String itemType, Long itemId) {
        InventoryStock stock = mapper.findStock(itemType, itemId);
        if (stock == null) {
            throw new ResourceNotFoundException("商品库存未配置");
        }
        return stock;
    }

    private InventoryReservation requireReservation(String orderId) {
        InventoryReservation reservation = mapper.findReservationByOrder(orderId);
        if (reservation == null) {
            throw new ResourceNotFoundException("库存预占记录不存在");
        }
        return reservation;
    }

    private java.util.List<InventoryReservation> requireReservations(String orderId) {
        var reservations = mapper.findReservations(orderId);
        if (reservations.isEmpty()) throw new ResourceNotFoundException("库存预占记录不存在");
        return reservations;
    }
}
