package com.acme.ClipCascade;

import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

import org.junit.jupiter.api.Test;

import com.acme.clipcascade.utils.ClipboardDataValidator;

/**
 * Unit tests for the application-level clipboard message validation.
 */
class ClipboardDataValidatorTests {

    @Test
    void knownTypesAreValid() {
        assertTrue(ClipboardDataValidator.isValidType("text"));
        assertTrue(ClipboardDataValidator.isValidType("image"));
        assertTrue(ClipboardDataValidator.isValidType("files"));
        assertTrue(ClipboardDataValidator.isValidType(" TEXT "));
        assertTrue(ClipboardDataValidator.isValidType("Image"));
    }

    @Test
    void unknownTypesAreInvalid() {
        assertFalse(ClipboardDataValidator.isValidType(null));
        assertFalse(ClipboardDataValidator.isValidType(""));
        assertFalse(ClipboardDataValidator.isValidType("executable"));
        assertFalse(ClipboardDataValidator.isValidType("text; rm -rf"));
    }

    @Test
    void payloadsUpToTheLimitAreAccepted() {
        assertTrue(ClipboardDataValidator.isWithinSizeLimit("dummy", 1024));
        assertTrue(ClipboardDataValidator.isWithinSizeLimit("a".repeat(1024), 1024));
    }

    @Test
    void payloadsOverTheLimitAreRejected() {
        assertFalse(ClipboardDataValidator.isWithinSizeLimit("a".repeat(1025), 1024));
        assertFalse(ClipboardDataValidator.isWithinSizeLimit(null, 1024));
    }
}
