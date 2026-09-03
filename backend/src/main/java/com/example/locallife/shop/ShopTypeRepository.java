package com.example.locallife.shop;

import org.springframework.stereotype.Repository;

import java.util.List;
import java.util.Optional;

@Repository
public class ShopTypeRepository {

    private final ShopTypeMapper mapper;

    public ShopTypeRepository(ShopTypeMapper mapper) {
        this.mapper = mapper;
    }

    public List<ShopType> findAll() {
        return mapper.findAll();
    }

    public Optional<ShopType> findById(Long id) {
        return Optional.ofNullable(mapper.findById(id));
    }
}
