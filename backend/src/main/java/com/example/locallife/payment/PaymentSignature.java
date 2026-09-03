package com.example.locallife.payment;

import com.example.locallife.common.ForbiddenOperationException;
import com.example.locallife.common.InvalidBusinessStateException;
import org.springframework.stereotype.Component;
import org.springframework.beans.factory.annotation.Autowired;

import javax.crypto.Mac;
import javax.crypto.spec.SecretKeySpec;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.time.Clock;
import java.time.Duration;
import java.time.Instant;
import java.util.HexFormat;

@Component
public class PaymentSignature {
    private final PaymentProperties properties;
    private final Clock clock;

    @Autowired
    public PaymentSignature(PaymentProperties properties) {
        this(properties, Clock.systemUTC());
    }

    PaymentSignature(PaymentProperties properties, Clock clock) {
        this.properties = properties;
        this.clock = clock;
    }

    public void verify(PaymentCallbackRequest request, String signature) {
        if (properties.callbackSecret() == null || properties.callbackSecret().isBlank()) {
            throw new IllegalStateException("PAYMENT_CALLBACK_SECRET 未配置，拒绝接收支付回调");
        }
        long now = Instant.now(clock).getEpochSecond();
        if (Math.abs(now - request.timestamp()) > properties.callbackTolerance().toSeconds()) {
            throw new ForbiddenOperationException("支付回调已过期");
        }
        String expected = sign(request);
        if (signature == null || !MessageDigest.isEqual(
                expected.getBytes(StandardCharsets.US_ASCII),
                signature.getBytes(StandardCharsets.US_ASCII))) {
            throw new ForbiddenOperationException("支付回调签名无效");
        }
    }

    public String sign(PaymentCallbackRequest request) {
        if (properties.callbackSecret() == null || properties.callbackSecret().isBlank()) {
            throw new InvalidBusinessStateException("支付回调密钥未配置");
        }
        String canonical = String.join("|",
                request.eventId(),
                request.paymentNo(),
                request.providerTradeNo(),
                request.amountMinor().toString(),
                request.status(),
                request.timestamp().toString());
        try {
            Mac mac = Mac.getInstance("HmacSHA256");
            mac.init(new SecretKeySpec(
                    properties.callbackSecret().getBytes(StandardCharsets.UTF_8), "HmacSHA256"));
            return HexFormat.of().formatHex(
                    mac.doFinal(canonical.getBytes(StandardCharsets.UTF_8)));
        } catch (Exception exception) {
            throw new IllegalStateException("无法生成支付签名", exception);
        }
    }
}
