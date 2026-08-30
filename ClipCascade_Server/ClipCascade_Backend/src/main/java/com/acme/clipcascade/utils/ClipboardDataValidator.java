package com.acme.clipcascade.utils;

import java.nio.charset.StandardCharsets;
import java.util.Locale;

import com.acme.clipcascade.constants.ServerConstants;

/**
 * Application-level validation for clipboard messages before they are relayed.
 *
 * Complements the transport-level WebSocket size limits (which kill the
 * connection) by rejecting individual messages with an error instead. Never
 * includes payload content in errors or logs.
 */
public class ClipboardDataValidator {

    public static boolean isValidType(String type) {
        if (type == null) {
            return false;
        }

        return ServerConstants.CLIPBOARD_DATA_TYPES.contains(type.strip().toLowerCase(Locale.ROOT));
    }

    public static boolean isWithinSizeLimit(String payload, long maxBytes) {
        if (payload == null) {
            return false;
        }

        return payload.getBytes(StandardCharsets.UTF_8).length <= maxBytes;
    }

    private ClipboardDataValidator() {
        // private constructor to prevent instantiation
    }
}
