package com.example.locallife.product;

import org.apache.ibatis.annotations.Mapper;
import org.apache.ibatis.annotations.Insert;
import org.apache.ibatis.annotations.Param;
import org.apache.ibatis.annotations.Select;
import org.apache.ibatis.annotations.SelectProvider;
import org.apache.ibatis.annotations.Update;

import java.util.List;

@Mapper
interface ProductMapper {

    String COLUMNS = """
            id, source, source_item_id, title, brand, seller, category_l1, category_l2,
            category_l3, snapshot_price_minor, currency, price_status, lifecycle_status,
            entity_version, attribute_text,
            data_nature, dataset_revision, source_license, provenance_url, imported_at
            """;

    @SelectProvider(type = ProductSqlProvider.class, method = "findByFilters")
    List<Product> findByFilters(
            @Param("query") String query,
            @Param("category") String category,
            @Param("brand") String brand,
            @Param("minPriceMinor") Long minPriceMinor,
            @Param("maxPriceMinor") Long maxPriceMinor,
            @Param("limit") int limit
    );

    @Select("SELECT " + COLUMNS + " FROM product WHERE id = #{id}")
    Product findById(@Param("id") Long id);

    @Insert("""
            INSERT INTO product(
                id, source, source_item_id, title, brand, seller,
                category_l1, category_l2, category_l3,
                snapshot_price_minor, currency, price_status,
                lifecycle_status, entity_version, attribute_text,
                data_nature, dataset_revision, source_license, provenance_url
            ) VALUES (
                #{id}, #{source}, #{sourceItemId}, #{title}, #{brand}, #{seller},
                #{categoryL1}, #{categoryL2}, #{categoryL3},
                #{snapshotPriceMinor}, #{currency}, #{priceStatus},
                'ACTIVE', 1, #{attributeText},
                #{dataNature}, #{datasetRevision}, #{sourceLicense}, #{provenanceUrl}
            )
            """)
    int insert(CreateProductRequest request);

    @Select("""
        SELECT
        """ + COLUMNS + """
        FROM product
        WHERE id > #{afterId}
        ORDER BY id ASC
        LIMIT #{limit}
        """)
    List<Product> findByIdAfter(@Param("afterId") Long afterId, @Param("limit") int limit);

    @Select("SELECT " + COLUMNS + " FROM product WHERE lifecycle_status = #{status} ORDER BY id LIMIT #{limit}")
    List<Product> findByLifecycleStatus(
            @Param("status") String status,
            @Param("limit") int limit
    );

    @Select("""
            SELECT attribute_key, value_type, raw_value, normalized_text, normalized_number,
                   normalized_boolean, unit, evidence_field, extraction_method, confidence
            FROM product_attribute
            WHERE product_id = #{productId}
            ORDER BY attribute_key
            """)
    List<ProductAttribute> findAttributes(@Param("productId") Long productId);

    @Select("""
            SELECT p.id AS product_id, p.snapshot_price_minor, p.price_status,
                   p.lifecycle_status, p.entity_version,
                   stock.available_quantity, stock.version AS inventory_version
            FROM product p
            LEFT JOIN inventory_stock stock
              ON stock.item_type = 'PRODUCT' AND stock.item_id = p.id
            WHERE p.id = #{productId}
            """)
    ProductCommerceFacts findCommerceFacts(@Param("productId") Long productId);

    @Update("""
            <script>
            UPDATE product
            <set>
                <if test="request.title != null">title = #{request.title},</if>
                <if test="request.brand != null">brand = #{request.brand},</if>
                <if test="request.seller != null">seller = #{request.seller},</if>
                <if test="request.categoryL1 != null">category_l1 = #{request.categoryL1},</if>
                <if test="request.categoryL2 != null">category_l2 = #{request.categoryL2},</if>
                <if test="request.categoryL3 != null">category_l3 = #{request.categoryL3},</if>
                <if test="request.snapshotPriceMinor != null">snapshot_price_minor = #{request.snapshotPriceMinor},</if>
                <if test="request.currency != null">currency = #{request.currency},</if>
                <if test="request.priceStatus != null">price_status = #{request.priceStatus},</if>
                <if test="request.attributeText != null">attribute_text = #{request.attributeText},</if>
                entity_version = entity_version + 1,
                updated_at = CURRENT_TIMESTAMP
            </set>
            WHERE id = #{id}
              AND entity_version = #{request.expectedVersion}
              AND lifecycle_status = 'ACTIVE'
            </script>
            """)
    int update(
            @Param("id") Long id,
            @Param("request") ProductUpdateRequest request
    );

    @Update("""
            UPDATE product
            SET lifecycle_status = 'DELETED',
                entity_version = entity_version + 1,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = #{id}
              AND entity_version = #{expectedVersion}
              AND lifecycle_status = 'ACTIVE'
            """)
    int softDelete(
            @Param("id") Long id,
            @Param("expectedVersion") Long expectedVersion
    );
}
