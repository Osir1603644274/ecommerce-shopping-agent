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
        InventoryReservation existing = mapper.findReservationByOrder(orderId);
        if (existing != null) {
            return existing;
        }
        InventoryStock stock = requireStock(itemType, itemId);
        if (mapper.reserve(stock.id(), quantity) != 1) {
            throw new BusinessConflictException("库存不足");
        }
        String reservationId = UUID.randomUUID().toString();
        mapper.insertReservation(reservationId, orderId, stock.id(), quantity, expiresAt);
        return mapper.findReservationByOrder(orderId);
    }

    @Transactional
    public void confirm(String orderId) {
        InventoryReservation reservation = requireReservation(orderId);
        if ("CONFIRMED".equals(reservation.status())) {
            return;
        }
        if (!"RESERVED".equals(reservation.status())
                || mapper.confirm(reservation.stockId(), reservation.quantity()) != 1
                || mapper.transitionReservation(orderId, "CONFIRMED") != 1) {
            throw new InvalidBusinessStateException("库存预占状态不允许确认");
        }
    }

    @Transactional
    public void release(String orderId, boolean expired) {
        InventoryReservation reservation = requireReservation(orderId);
        if ("RELEASED".equals(reservation.status()) || "EXPIRED".equals(reservation.status())) {
            return;
        }
        if (!"RESERVED".equals(reservation.status())
                || mapper.release(reservation.stockId(), reservation.quantity()) != 1
                || mapper.transitionReservation(orderId, expired ? "EXPIRED" : "RELEASED") != 1) {
            throw new InvalidBusinessStateException("库存预占状态不允许释放");
        }
    }

    public InventoryStock getStock(String itemType, Long itemId) {
        return requireStock(itemType, itemId);
    }

    @Transactional
    public InventoryStock createStock(String itemType, Long itemId, int quantity) {
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
}
