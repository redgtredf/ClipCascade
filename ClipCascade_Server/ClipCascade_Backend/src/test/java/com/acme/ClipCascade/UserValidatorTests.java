package com.acme.ClipCascade;

import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

import org.junit.jupiter.api.Test;

import com.acme.clipcascade.utils.UserValidator;

/**
 * Server-side password policy: the submitted value (normally the client's
 * SHA3-512 hex of the raw password) must fall inside the 8..128 window.
 */
class UserValidatorTests {

    @Test
    void passwordPolicyAcceptsClientHashedValues() {
        // SHA3-512 hex is 128 characters
        String sha3Hex = "a".repeat(128);
        assertTrue(UserValidator.isValidPassword(sha3Hex));
    }

    @Test
    void passwordPolicyEnforcesMinimumLength() {
        assertFalse(UserValidator.isValidPassword(null));
        assertFalse(UserValidator.isValidPassword(""));
        assertFalse(UserValidator.isValidPassword("1234567")); // 7 chars
        assertTrue(UserValidator.isValidPassword("12345678")); // 8 chars
    }

    @Test
    void passwordPolicyEnforcesMaximumLength() {
        assertTrue(UserValidator.isValidPassword("a".repeat(128)));
        assertFalse(UserValidator.isValidPassword("a".repeat(129)));
    }
}
