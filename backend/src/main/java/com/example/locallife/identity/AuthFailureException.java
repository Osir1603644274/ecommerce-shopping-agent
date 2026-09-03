package com.example.locallife.identity;

import org.springframework.http.HttpStatus;

class AuthFailureException extends RuntimeException {

    private final HttpStatus status;

    AuthFailureException(HttpStatus status, String message) {
        super(message);
        this.status = status;
    }

    HttpStatus status() {
        return status;
    }
}
