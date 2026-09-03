package com.example.locallife.product;

import com.example.locallife.common.ApiResponse;
import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.databind.DeserializationFeature;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import jakarta.validation.ConstraintViolation;
import jakarta.validation.Validator;
import jakarta.validation.constraints.Min;
import org.springframework.http.HttpStatus;
import org.springframework.validation.annotation.Validated;
import org.springframework.web.bind.annotation.DeleteMapping;
import org.springframework.web.bind.annotation.PatchMapping;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;
import org.springframework.web.server.ResponseStatusException;

import java.util.Comparator;

@Validated
@RestController
@RequestMapping("/api/admin/products")
public class ProductAdminController {
    private final ProductAdminService service;
    private final ObjectMapper json;
    private final Validator validator;

    public ProductAdminController(ProductAdminService service, ObjectMapper json, Validator validator) {
        this.service = service;
        this.json = json;
        this.validator = validator;
    }

    @PostMapping
    public org.springframework.http.ResponseEntity<ApiResponse<ProductMutationReceipt>> create(
            @RequestBody JsonNode body
    ) {
        return org.springframework.http.ResponseEntity.status(HttpStatus.CREATED)
                .body(ApiResponse.ok(service.create(parseCreate(body))));
    }

    @PatchMapping("/{id}")
    public ApiResponse<ProductMutationReceipt> update(
            @PathVariable @Min(value = 1, message = "商品 ID 必须大于 0") Long id,
            @RequestBody JsonNode body
    ) {
        return ApiResponse.ok(service.update(id, parse(body)));
    }

    @DeleteMapping("/{id}")
    public ApiResponse<ProductMutationReceipt> delete(
            @PathVariable @Min(value = 1, message = "商品 ID 必须大于 0") Long id,
            @RequestParam @Min(value = 1, message = "expectedVersion 必须大于 0") Long expectedVersion
    ) {
        return ApiResponse.ok(service.delete(id, expectedVersion));
    }

    private ProductUpdateRequest parse(JsonNode body) {
        try {
            ProductUpdateRequest request = json.copy()
                    .enable(DeserializationFeature.FAIL_ON_UNKNOWN_PROPERTIES)
                    .treeToValue(body, ProductUpdateRequest.class);
            validator.validate(request).stream()
                    .map(ConstraintViolation::getMessage)
                    .min(Comparator.naturalOrder())
                    .ifPresent(message -> {
                        throw new ResponseStatusException(HttpStatus.BAD_REQUEST, message);
                    });
            return request;
        } catch (JsonProcessingException | IllegalArgumentException exception) {
            throw new ResponseStatusException(HttpStatus.BAD_REQUEST, "请求体格式错误");
        }
    }

    private CreateProductRequest parseCreate(JsonNode body) {
        try {
            CreateProductRequest request = json.copy()
                    .enable(DeserializationFeature.FAIL_ON_UNKNOWN_PROPERTIES)
                    .treeToValue(body, CreateProductRequest.class);
            validator.validate(request).stream()
                    .map(ConstraintViolation::getMessage)
                    .min(Comparator.naturalOrder())
                    .ifPresent(message -> {
                        throw new ResponseStatusException(HttpStatus.BAD_REQUEST, message);
                    });
            return request;
        } catch (JsonProcessingException | IllegalArgumentException exception) {
            throw new ResponseStatusException(HttpStatus.BAD_REQUEST, "请求体格式错误");
        }
    }
}
