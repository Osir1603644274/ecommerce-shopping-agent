package com.example.locallife.identity;

import com.example.locallife.common.ApiResponse;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.ExceptionHandler;
import org.springframework.web.bind.annotation.RestControllerAdvice;

@RestControllerAdvice
class AuthExceptionHandler {

    @ExceptionHandler(AuthFailureException.class)
    ResponseEntity<ApiResponse<Void>> handleAuthFailure(AuthFailureException exception) {
        return ResponseEntity
                .status(exception.status())
                .body(ApiResponse.fail(exception.getMessage()));
    }
}
