package com.example.locallife.common;

public class InvalidBusinessStateException extends RuntimeException {

    public InvalidBusinessStateException(String message) {
        super(message);
    }
}
