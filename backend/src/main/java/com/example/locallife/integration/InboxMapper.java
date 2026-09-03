package com.example.locallife.integration;

import org.apache.ibatis.annotations.Insert;
import org.apache.ibatis.annotations.Mapper;
import org.apache.ibatis.annotations.Param;
import org.apache.ibatis.annotations.Select;
import org.apache.ibatis.annotations.Update;

import java.time.LocalDateTime;

@Mapper
interface InboxMapper {

    @Insert("""
            INSERT INTO inbox_event(
                consumer_name, event_id, event_type, payload_hash,
                status, attempts, claimed_at
            ) VALUES(
                #{consumer}, #{event.id}, #{event.eventType}, #{payloadHash},
                'PROCESSING', 1, #{now}
            )
            """)
    int insertClaim(
            @Param("consumer") String consumer,
            @Param("event") EventEnvelope event,
            @Param("payloadHash") String payloadHash,
            @Param("now") LocalDateTime now
    );

    @Select("""
            SELECT id, consumer_name, event_id, event_type, payload_hash,
                   status, attempts, last_error, claimed_at, processed_at
            FROM inbox_event
            WHERE consumer_name = #{consumer} AND event_id = #{eventId}
            """)
    InboxEvent find(
            @Param("consumer") String consumer,
            @Param("eventId") String eventId
    );

    @Update("""
            UPDATE inbox_event
            SET status = 'PROCESSING', attempts = attempts + 1,
                claimed_at = #{now}, last_error = NULL,
                updated_at = CURRENT_TIMESTAMP
            WHERE consumer_name = #{consumer} AND event_id = #{eventId}
              AND (
                status = 'FAILED'
                OR (status = 'PROCESSING' AND claimed_at < #{staleBefore})
              )
            """)
    int reclaim(
            @Param("consumer") String consumer,
            @Param("eventId") String eventId,
            @Param("now") LocalDateTime now,
            @Param("staleBefore") LocalDateTime staleBefore
    );

    @Update("""
            UPDATE inbox_event
            SET status = 'PROCESSED', processed_at = #{now},
                last_error = NULL, updated_at = CURRENT_TIMESTAMP
            WHERE consumer_name = #{consumer} AND event_id = #{eventId}
              AND status = 'PROCESSING'
            """)
    int markProcessed(
            @Param("consumer") String consumer,
            @Param("eventId") String eventId,
            @Param("now") LocalDateTime now
    );

    @Update("""
            UPDATE inbox_event
            SET status = 'FAILED', last_error = #{error},
                updated_at = CURRENT_TIMESTAMP
            WHERE consumer_name = #{consumer} AND event_id = #{eventId}
              AND status = 'PROCESSING'
            """)
    int markFailed(
            @Param("consumer") String consumer,
            @Param("eventId") String eventId,
            @Param("error") String error
    );

    @Update("""
            UPDATE inbox_event
            SET status = 'DEAD', updated_at = CURRENT_TIMESTAMP
            WHERE consumer_name = #{consumer} AND event_id = #{eventId}
              AND status = 'FAILED'
            """)
    int markDead(
            @Param("consumer") String consumer,
            @Param("eventId") String eventId
    );

    @Insert("""
            INSERT INTO dead_letter_event(
                source, event_id, event_type, payload_json, attempts, last_error
            ) VALUES(
                #{source}, #{event.id}, #{event.eventType}, #{event.payloadJson},
                #{attempts}, #{error}
            )
            """)
    int insertDeadLetter(
            @Param("source") String source,
            @Param("event") EventEnvelope event,
            @Param("attempts") int attempts,
            @Param("error") String error
    );

    @Select("SELECT COUNT(*) FROM inbox_event WHERE status = #{status}")
    int countByStatus(@Param("status") String status);
}
