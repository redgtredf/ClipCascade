package com.acme.ClipCascade;

import static org.junit.jupiter.api.Assertions.assertFalse;

import org.junit.jupiter.api.Test;
import org.springframework.test.util.ReflectionTestUtils;

import com.acme.clipcascade.config.ClipCascadeProperties;

/**
 * Asserts that ClipCascadeProperties.toString() (rendered on the admin UI and
 * usable in debug logging) never contains secret values. Dummy values only.
 */
class ClipCascadePropertiesRedactionTests {

    private static final String DUMMY_DB_PASSWORD = "dummy-db-password-value";
    private static final String DUMMY_DB_USERNAME = "dummy-db-username-value";
    private static final String DUMMY_BROKER_USERNAME = "dummy-broker-username-value";
    private static final String DUMMY_BROKER_PASSWORD = "dummy-broker-password-value";
    private static final String DUMMY_ADMIN_USERNAME = "dummy-admin-username-value";
    private static final String DUMMY_ADMIN_PASSWORD = "dummy-admin-password-value";

    private ClipCascadeProperties propertiesWithSecrets() {
        ClipCascadeProperties properties = new ClipCascadeProperties();
        ReflectionTestUtils.setField(properties, "serverDbUsername", DUMMY_DB_USERNAME);
        ReflectionTestUtils.setField(properties, "serverDbPassword", DUMMY_DB_PASSWORD);
        ReflectionTestUtils.setField(properties, "brokerUsername", DUMMY_BROKER_USERNAME);
        ReflectionTestUtils.setField(properties, "brokerPassword", DUMMY_BROKER_PASSWORD);
        ReflectionTestUtils.setField(properties, "adminUsername", DUMMY_ADMIN_USERNAME);
        ReflectionTestUtils.setField(properties, "adminPassword", DUMMY_ADMIN_PASSWORD);
        return properties;
    }

    @Test
    void toStringDoesNotContainDbPassword() {
        assertFalse(propertiesWithSecrets().toString().contains(DUMMY_DB_PASSWORD));
    }

    @Test
    void toStringDoesNotContainBrokerPassword() {
        assertFalse(propertiesWithSecrets().toString().contains(DUMMY_BROKER_PASSWORD));
    }

    @Test
    void toStringDoesNotContainAdminPassword() {
        assertFalse(propertiesWithSecrets().toString().contains(DUMMY_ADMIN_PASSWORD));
    }

    @Test
    void toStringDoesNotContainCredentialUsernames() {
        String asString = propertiesWithSecrets().toString();
        assertFalse(asString.contains(DUMMY_DB_USERNAME));
        assertFalse(asString.contains(DUMMY_BROKER_USERNAME));
        assertFalse(asString.contains(DUMMY_ADMIN_USERNAME));
    }
}
