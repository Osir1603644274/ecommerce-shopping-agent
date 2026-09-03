package com.example.locallife.memory;

import org.apache.ibatis.annotations.*;
import java.time.LocalDateTime;
import java.util.List;

@Mapper
interface MemoryMapper {
    @Select("SELECT id,owner_user_id,category,semantic_key,token_value,version,status,expires_at,supersedes_id,command_digest,content_digest FROM user_shopping_memory WHERE owner_user_id=#{owner} AND category=#{category} AND semantic_key=#{key} ORDER BY version DESC LIMIT 1 FOR UPDATE")
    ShoppingMemory latestForUpdate(@Param("owner") String owner, @Param("category") String category, @Param("key") String key);
    @Insert("INSERT INTO user_memory_consent_consumption(owner_user_id,consent_event_id,command_digest,content_digest) VALUES(#{owner},#{event},#{commandDigest},#{contentDigest})")
    int consumeConsent(@Param("owner") String owner, @Param("event") String event, @Param("commandDigest") String commandDigest, @Param("contentDigest") String contentDigest);
    @Insert("INSERT INTO user_shopping_memory(id,owner_user_id,category,semantic_key,token_value,version,status,expires_at,supersedes_id,command_digest,content_digest) VALUES(#{m.id},#{m.ownerUserId},#{m.category},#{m.semanticKey},#{m.tokenValue},#{m.version},#{m.status},#{m.expiresAt},#{m.supersedesId},#{m.commandDigest},#{m.contentDigest})")
    int insert(@Param("m") ShoppingMemory memory);
    @Insert("INSERT INTO user_memory_audit(owner_user_id,memory_id,operation,outcome,command_digest,consent_event_id) VALUES(#{owner},#{memoryId},#{operation},'APPLIED',#{commandDigest},#{event})")
    int audit(@Param("owner") String owner, @Param("memoryId") String memoryId, @Param("operation") String operation, @Param("commandDigest") String commandDigest, @Param("event") String event);
    @Insert("INSERT INTO user_memory_projection_head(owner_user_id,revision) VALUES(#{owner},1) ON DUPLICATE KEY UPDATE revision=revision+1")
    int advanceProjectionHead(@Param("owner") String owner);
    @Select("SELECT revision FROM user_memory_projection_head WHERE owner_user_id=#{owner}")
    Long projectionRevision(@Param("owner") String owner);
    @Select("SELECT id,owner_user_id,category,semantic_key,token_value,version,status,expires_at,supersedes_id,command_digest,content_digest FROM user_shopping_memory m WHERE m.owner_user_id=#{owner} AND m.category='shopping_preference' AND m.status='ACTIVE' AND m.expires_at>#{now} AND NOT EXISTS (SELECT 1 FROM user_shopping_memory n WHERE n.owner_user_id=m.owner_user_id AND n.category=m.category AND n.semantic_key=m.semantic_key AND n.version>m.version) ORDER BY m.category,m.semantic_key,m.version LIMIT 8")
    List<ShoppingMemory> projection(@Param("owner") String owner, @Param("now") LocalDateTime now);

    @Select("""
            SELECT id, owner_user_id, memory_category, product_category,
                   recipient_scope, source, data_class, semantic_key,
                   token_value, version, status, created_at, updated_at,
                   expires_at, supersedes_id
            FROM user_shopping_memory m
            WHERE m.owner_user_id=#{owner}
              AND m.category='shopping_preference'
              AND m.memory_category='shopping_preference'
              AND m.product_category IN ('phone','laptop','headphones')
              AND m.recipient_scope='self'
              AND m.source IN ('explicit_user','user_confirmed')
              AND m.data_class='long_term_preference'
              AND m.status='ACTIVE'
              AND m.expires_at>#{now}
              AND NOT EXISTS (
                  SELECT 1 FROM user_shopping_memory n
                  WHERE n.owner_user_id=m.owner_user_id
                    AND n.memory_category=m.memory_category
                    AND n.product_category=m.product_category
                    AND n.recipient_scope=m.recipient_scope
                    AND n.semantic_key=m.semantic_key
                    AND n.version>m.version
              )
            ORDER BY m.updated_at DESC, m.product_category,
                     m.semantic_key, m.id
            LIMIT 65
            """)
    List<ExplicitPreferenceMemory> projectionV2Candidates(
            @Param("owner") String owner,
            @Param("now") LocalDateTime now
    );

    @Select("""
            SELECT id, owner_user_id, memory_category, product_category,
                   recipient_scope, source, data_class, semantic_key,
                   token_value, version, status, created_at, updated_at,
                   expires_at, supersedes_id
            FROM user_shopping_memory
            WHERE owner_user_id=#{owner}
              AND memory_category=#{memoryCategory}
              AND product_category=#{productCategory}
              AND recipient_scope=#{recipientScope}
              AND semantic_key=#{semanticKey}
            ORDER BY version, id
            """)
    List<ExplicitPreferenceMemory> projectionV2Chain(
            @Param("owner") String owner,
            @Param("memoryCategory") String memoryCategory,
            @Param("productCategory") String productCategory,
            @Param("recipientScope") String recipientScope,
            @Param("semanticKey") String semanticKey
    );

    @Select("""
            SELECT id, owner_user_id, memory_category, product_category,
                   recipient_scope, source, data_class, semantic_key,
                   token_value, version, status, created_at, updated_at,
                   expires_at, supersedes_id
            FROM user_shopping_memory
            WHERE owner_user_id=#{owner}
              AND memory_category='shopping_preference'
              AND product_category=#{productCategory}
              AND recipient_scope=#{recipientScope}
              AND semantic_key=#{semanticKey}
            ORDER BY version DESC, id DESC
            LIMIT 1 FOR UPDATE
            """)
    ExplicitPreferenceMemory latestV2ForUpdate(
            @Param("owner") String owner,
            @Param("productCategory") String productCategory,
            @Param("recipientScope") String recipientScope,
            @Param("semanticKey") String semanticKey
    );

    @Insert("""
            INSERT INTO user_memory_consent_grant(
                consent_event_id, owner_user_id, command_id, consent_action,
                command_digest, content_digest, expires_at
            ) VALUES(
                #{eventId}, #{owner}, #{commandId}, #{action},
                #{commandDigest}, #{contentDigest}, #{expiresAt}
            )
            """)
    int issueV2Consent(
            @Param("eventId") String eventId,
            @Param("owner") String owner,
            @Param("commandId") String commandId,
            @Param("action") String action,
            @Param("commandDigest") String commandDigest,
            @Param("contentDigest") String contentDigest,
            @Param("expiresAt") LocalDateTime expiresAt
    );

    @Insert("""
            INSERT IGNORE INTO user_memory_command_result(
                owner_user_id, command_id, request_digest, operation, status
            ) VALUES(#{owner}, #{commandId}, #{requestDigest}, #{operation}, 'PROCESSING')
            """)
    int reserveV2Command(
            @Param("owner") String owner,
            @Param("commandId") String commandId,
            @Param("requestDigest") String requestDigest,
            @Param("operation") String operation
    );

    @Select("""
            SELECT owner_user_id, command_id, request_digest, operation, status,
                   memory_id, memory_version, memory_status
            FROM user_memory_command_result
            WHERE owner_user_id=#{owner} AND command_id=#{commandId}
            FOR UPDATE
            """)
    MemoryCommandResult v2CommandResultForUpdate(
            @Param("owner") String owner,
            @Param("commandId") String commandId
    );

    @Update("""
            UPDATE user_memory_consent_grant
            SET consumed_at=UTC_TIMESTAMP()
            WHERE consent_event_id=#{eventId}
              AND owner_user_id=#{owner}
              AND command_id=#{commandId}
              AND consent_action=#{action}
              AND command_digest=#{commandDigest}
              AND content_digest=#{contentDigest}
              AND consumed_at IS NULL
              AND expires_at>UTC_TIMESTAMP()
            """)
    int consumeV2Consent(
            @Param("eventId") String eventId,
            @Param("owner") String owner,
            @Param("commandId") String commandId,
            @Param("action") String action,
            @Param("commandDigest") String commandDigest,
            @Param("contentDigest") String contentDigest
    );

    @Insert("""
            INSERT INTO user_shopping_memory(
                id, owner_user_id, category, memory_category, product_category,
                recipient_scope, source, data_class, semantic_key, token_value,
                version, status, expires_at, supersedes_id, command_digest,
                content_digest
            ) VALUES(
                #{m.id}, #{m.ownerUserId}, 'shopping_preference',
                'shopping_preference', #{m.productCategory}, #{m.recipientScope},
                #{m.source}, 'long_term_preference', #{m.semanticKey},
                #{m.tokenValue}, #{m.version}, #{m.status}, #{m.expiresAt},
                #{m.supersedesId}, #{m.commandDigest}, #{m.contentDigest}
            )
            """)
    int insertV2(@Param("m") ExplicitMemoryWrite memory);

    @Update("""
            UPDATE user_memory_command_result
            SET status='APPLIED', memory_id=#{memoryId}, memory_version=#{version},
                memory_status=#{memoryStatus}, completed_at=UTC_TIMESTAMP()
            WHERE owner_user_id=#{owner} AND command_id=#{commandId}
              AND request_digest=#{requestDigest} AND status='PROCESSING'
            """)
    int completeV2Command(
            @Param("owner") String owner,
            @Param("commandId") String commandId,
            @Param("requestDigest") String requestDigest,
            @Param("memoryId") String memoryId,
            @Param("version") int version,
            @Param("memoryStatus") String memoryStatus
    );

    @Select("""
            SELECT COUNT(*)
            FROM shopping_memory_catalog_value
            WHERE catalog_revision=#{catalogRevision}
              AND category_id=#{categoryId}
              AND attribute_key=#{attributeKey}
              AND normalized_value=#{normalizedValue}
              AND active=TRUE
            """)
    int catalogValueExists(
            @Param("catalogRevision") String catalogRevision,
            @Param("categoryId") String categoryId,
            @Param("attributeKey") String attributeKey,
            @Param("normalizedValue") String normalizedValue
    );

    @Select("""
            SELECT display_label
            FROM shopping_memory_catalog_value
            WHERE catalog_revision=#{catalogRevision}
              AND category_id=#{categoryId}
              AND attribute_key=#{attributeKey}
              AND normalized_value=#{normalizedValue}
              AND active=TRUE
            LIMIT 1
            """)
    String catalogValueLabel(
            @Param("catalogRevision") String catalogRevision,
            @Param("categoryId") String categoryId,
            @Param("attributeKey") String attributeKey,
            @Param("normalizedValue") String normalizedValue
    );

    @Select("""
            SELECT id, owner_user_id, product_category AS category_id,
                   recipient_scope, source, schema_version, preference_kind,
                   catalog_revision, semantic_key AS attribute_key,
                   token_value AS normalized_value, version, status, created_at,
                   updated_at, expires_at, supersedes_id
            FROM user_shopping_memory
            WHERE owner_user_id=#{owner}
              AND schema_version=3
              AND product_category=#{categoryId}
              AND recipient_scope=#{recipientScope}
              AND semantic_key=#{attributeKey}
            ORDER BY version DESC, id DESC
            LIMIT 1 FOR UPDATE
            """)
    CatalogPreferenceMemory latestV3ForUpdate(
            @Param("owner") String owner,
            @Param("categoryId") String categoryId,
            @Param("recipientScope") String recipientScope,
            @Param("attributeKey") String attributeKey
    );

    @Insert("""
            INSERT INTO user_shopping_memory(
                id, owner_user_id, category, memory_category, product_category,
                recipient_scope, source, data_class, schema_version,
                preference_kind, catalog_revision, semantic_key, token_value,
                version, status, expires_at, supersedes_id, command_digest,
                content_digest
            ) VALUES(
                #{m.id}, #{m.ownerUserId}, 'shopping_preference',
                'shopping_preference', #{m.categoryId}, #{m.recipientScope},
                #{m.source}, 'long_term_preference', 3, #{m.preferenceKind},
                #{m.catalogRevision}, #{m.attributeKey}, #{m.normalizedValue},
                #{m.version}, #{m.status}, #{m.expiresAt}, #{m.supersedesId},
                #{m.commandDigest}, #{m.contentDigest}
            )
            """)
    int insertV3(@Param("m") CatalogMemoryWrite memory);

    @Select("""
            SELECT id, owner_user_id, product_category AS category_id,
                   recipient_scope, source, schema_version, preference_kind,
                   catalog_revision, semantic_key AS attribute_key,
                   token_value AS normalized_value, version, status, created_at,
                   updated_at, expires_at, supersedes_id
            FROM user_shopping_memory m
            WHERE m.owner_user_id=#{owner}
              AND m.schema_version=3
              AND m.memory_category='shopping_preference'
              AND m.recipient_scope='self'
              AND m.source='user_confirmed'
              AND m.data_class='long_term_preference'
              AND m.status='ACTIVE'
              AND m.expires_at>#{now}
              AND NOT EXISTS (
                  SELECT 1 FROM user_shopping_memory n
                  WHERE n.owner_user_id=m.owner_user_id
                    AND n.schema_version=m.schema_version
                    AND n.product_category=m.product_category
                    AND n.recipient_scope=m.recipient_scope
                    AND n.semantic_key=m.semantic_key
                    AND n.version>m.version
              )
            ORDER BY m.updated_at DESC, m.product_category,
                     m.semantic_key, m.id
            LIMIT 65
            """)
    List<CatalogPreferenceMemory> projectionV3Candidates(
            @Param("owner") String owner,
            @Param("now") LocalDateTime now
    );

    @Select("""
            SELECT id, owner_user_id, product_category AS category_id,
                   recipient_scope, source, schema_version, preference_kind,
                   catalog_revision, semantic_key AS attribute_key,
                   token_value AS normalized_value, version, status, created_at,
                   updated_at, expires_at, supersedes_id
            FROM user_shopping_memory
            WHERE owner_user_id=#{owner}
              AND schema_version=3
              AND product_category=#{categoryId}
              AND recipient_scope=#{recipientScope}
              AND semantic_key=#{attributeKey}
            ORDER BY version, id
            """)
    List<CatalogPreferenceMemory> projectionV3Chain(
            @Param("owner") String owner,
            @Param("categoryId") String categoryId,
            @Param("recipientScope") String recipientScope,
            @Param("attributeKey") String attributeKey
    );
}
