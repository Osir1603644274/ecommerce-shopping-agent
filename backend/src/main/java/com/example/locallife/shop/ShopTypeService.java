package com.example.locallife.shop;

import org.springframework.stereotype.Service;

import java.util.List;
import java.util.Optional;

@Service
public class ShopTypeService {

    private final ShopTypeRepository shopTypeRepository;

    public ShopTypeService(ShopTypeRepository shopTypeRepository) {
        this.shopTypeRepository = shopTypeRepository;
    }

    public List<ShopTypeResponse> listShopTypes(String keyword) {
        List<ShopType> shopTypes = shopTypeRepository.findAll();
        if (keyword == null || keyword.isBlank()) {
            return shopTypes.stream()
                    .map(this::toResponse)
                    .toList();
        }

        return shopTypes.stream()
                .filter(shopType -> shopType.name().contains(keyword.trim()))
                .map(this::toResponse)
                .toList();
    }

    public Optional<ShopTypeResponse> getShopType(Long id) {
        return shopTypeRepository.findById(id)
                .map(this::toResponse);
    }

    private ShopTypeResponse toResponse(ShopType shopType) {
        return new ShopTypeResponse(shopType.id(), shopType.name(), shopType.sort());
    }
}
