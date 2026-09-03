package com.example.locallife.integration;

import org.apache.ibatis.annotations.Insert;
import org.apache.ibatis.annotations.Mapper;
import org.apache.ibatis.annotations.Param;
import org.apache.ibatis.annotations.Select;
import org.apache.ibatis.annotations.Update;

import java.time.LocalDateTime;
import java.util.List;

@Mapper
interface OutboxMapper {
    String COLUMNS = """
            id, aggregate_type, aggregate_id, event_type, payload_json, status,
            attempts, next_attempt_at, lock_owner, locked_at, published_at,
            last_error, occurred_at
            """;

    @Insert("""
            INSERT INTO outbox_event(
                id, aggregate_type, aggregate_id, event_type, payload_json,
                status, attempts, next_attempt_at, occurred_at
            ) VALUES(
                #{event.id}, #{event.aggregateType}, #{event.aggregateId},
                #{event.eventType}, #{event.payloadJson}, 'PENDING', 0,
                #{event.nextAttemptAt}, #{event.occurredAt}
            )
            """)
    int insert(@Param("event") OutboxEvent event);

    @Select("""
            SELECT id FROM outbox_event
            WHERE (
                status = 'PENDING'
                OR (status = 'PROCESSING' AND locked_at < #{staleBefore})
            )
              AND next_attempt_at <= #{now}
            ORDER BY occurred_at
            LIMIT #{limit}
            """)
    List<String> findDispatchableIds(
            @Param("now") LocalDateTime now,
            @Param("staleBefore") LocalDateTime staleBefore,
            @Param("limit") int limit
    );

    @Update("""
            UPDATE outbox_event
            SET status = 'PROCESSING', lock_owner = #{owner}, locked_at = #{now},
                updated_at = CURRENT_TIMESTAMP
            WHERE id = #{id}
              AND (
                status = 'PENDING'
                OR (status = 'PROCESSING' AND locked_at < #{staleBefore})
              )
              AND next_attempt_at <= #{now}
            """)
    int claim(
            @Param("id") String id,
            @Param("owner") String owner,
            @Param("now") LocalDateTime now,
            @Param("staleBefore") LocalDateTime staleBefore
    );

    @Select("SELECT " + COLUMNS + " FROM outbox_event WHERE id = #{id}")
    OutboxEvent findById(@Param("id") String id);

    @Update("""
            UPDATE outbox_event
            SET status = 'PUBLISHED', published_at = #{now}, lock_owner = NULL,
                locked_at = NULL, last_error = NULL, updated_at = CURRENT_TIMESTAMP
            WHERE id = #{id} AND status = 'PROCESSING' AND lock_owner = #{owner}
            """)
    int markPublished(
            @Param("id") String id,
            @Param("owner") String owner,
            @Param("now") LocalDateTime now
    );

    @Update("""
            UPDATE outbox_event
            SET status = #{status}, attempts = attempts + 1,
                next_attempt_at = #{nextAttemptAt}, last_error = #{error},
                lock_owner = NULL, locked_at = NULL, updated_at = CURRENT_TIMESTAMP
            WHERE id = #{id} AND status = 'PROCESSING' AND lock_owner = #{owner}
            """)
    int markFailure(
            @Param("id") String id,
            @Param("owner") String owner,
            @Param("status") String status,
            @Param("nextAttemptAt") LocalDateTime nextAttemptAt,
            @Param("error") String error
    );

    @Insert("""
            INSERT INTO dead_letter_event(
                source, event_id, event_type, payload_json, attempts, last_error
            ) VALUES(
                'OUTBOX', #{event.id}, #{event.eventType}, #{event.payloadJson},
                #{attempts}, #{error}
            )
            """)
    int insertDeadLetter(
            @Param("event") OutboxEvent event,
            @Param("attempts") int attempts,
            @Param("error") String error
    );

    @Select("SELECT COUNT(*) FROM outbox_event WHERE status = #{status}")
    int countByStatus(@Param("status") String status);
}
