package com.example.locallife.review;

import com.example.locallife.common.ApiResponse;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.ExceptionHandler;
import org.springframework.web.bind.annotation.RestControllerAdvice;

@RestControllerAdvice
class ReviewExceptionHandler {

    private static final Logger log = LoggerFactory.getLogger(ReviewExceptionHandler.class);

    @ExceptionHandler(ReviewVectorSyncException.class)
    ResponseEntity<ApiResponse<Void>> handleReviewVectorSync(ReviewVectorSyncException exception) {
        log.error("评论向量同步最终失败", exception);
        return ResponseEntity
                .status(HttpStatus.SERVICE_UNAVAILABLE)
                .body(ApiResponse.fail("评论向量同步失败，请稍后重试"));
    }
}
