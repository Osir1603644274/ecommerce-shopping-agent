package com.example.locallife.shop;

import org.springframework.stereotype.Repository;

import java.util.List;
import java.util.Optional;

@Repository
public class ShopRepository {

    private final ShopMapper mapper;

    public ShopRepository(ShopMapper mapper) {
        this.mapper = mapper;
    }

    public List<Shop> findAll() {
        return mapper.findAll();
    }

    public List<Shop> findByTypeId(Long typeId) {
        return mapper.findByTypeId(typeId);
    }

    public List<Shop> findByFilters(Long typeId, String name) {
        return mapper.findByFilters(typeId, name);
    }

    public Optional<Shop> findById(Long id) {
        return Optional.ofNullable(mapper.findById(id));
    }

    public int updateDetails(Long id, UpdateShopRequest request) {
        return mapper.updateDetails(id, request);
    }
}
