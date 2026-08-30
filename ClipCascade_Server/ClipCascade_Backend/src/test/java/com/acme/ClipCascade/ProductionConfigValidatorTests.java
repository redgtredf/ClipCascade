package com.acme.ClipCascade;

import static org.junit.jupiter.api.Assertions.assertDoesNotThrow;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

import org.junit.jupiter.api.Test;
import org.springframework.boot.SpringApplication;
import org.springframework.mock.env.MockEnvironment;

import com.acme.clipcascade.config.ProductionConfigValidator;

/**
 * Unit tests for the fail-fast production configuration validation.
 * Dummy values only.
 */
class ProductionConfigValidatorTests {

    private final ProductionConfigValidator validator = new ProductionConfigValidator();
    private final SpringApplication application = new SpringApplication();

    /** All mandatory values set with dummy data (plus database password). */
    private MockEnvironment validEnvironment() {
        MockEnvironment environment = baseEnvironment();
        environment.setProperty("CC_SERVER_DB_PASSWORD", "dummy-db-password");
        return environment;
    }

    /** Mandatory values except CC_SERVER_DB_PASSWORD. */
    private MockEnvironment baseEnvironment() {
        MockEnvironment environment = new MockEnvironment();
        environment.setProperty("CC_ADMIN_USERNAME", "dummy-admin");
        environment.setProperty("CC_ADMIN_PASSWORD", "dummy-admin-password");
        environment.setProperty("CC_ALLOWED_ORIGINS", "https://clipcascade.example.com");
        return environment;
    }

    @Test
    void validConfigurationPasses() {
        assertDoesNotThrow(() -> validator.postProcessEnvironment(validEnvironment(), application));
    }

    @Test
    void missingDatabasePasswordFailsStartup() {
        MockEnvironment environment = validEnvironment();
        environment.setProperty("CC_SERVER_DB_PASSWORD", "");

        IllegalStateException exception = assertThrows(IllegalStateException.class,
                () -> validator.postProcessEnvironment(environment, application));

        assertTrue(exception.getMessage().contains("CC_SERVER_DB_PASSWORD"));
    }

    @Test
    void absentDatabasePasswordFailsStartup() {
        MockEnvironment environment = baseEnvironment(); // CC_SERVER_DB_PASSWORD never set

        IllegalStateException exception = assertThrows(IllegalStateException.class,
                () -> validator.postProcessEnvironment(environment, application));

        assertTrue(exception.getMessage().contains("CC_SERVER_DB_PASSWORD"));
    }

    @Test
    void missingAdminCredentialsFailStartup() {
        MockEnvironment environment = validEnvironment();
        environment.setProperty("CC_ADMIN_USERNAME", " ");
        environment.setProperty("CC_ADMIN_PASSWORD", "");

        assertThrows(IllegalStateException.class,
                () -> validator.postProcessEnvironment(environment, application));

        MockEnvironment blankUsernameEnvironment = validEnvironment();
        blankUsernameEnvironment.setProperty("CC_ADMIN_USERNAME", "");

        IllegalStateException exception = assertThrows(IllegalStateException.class,
                () -> validator.postProcessEnvironment(blankUsernameEnvironment, application));

        assertTrue(exception.getMessage().contains("CC_ADMIN_USERNAME"));
    }

    @Test
    void externalBrokerWithoutCredentialsFailsStartup() {
        MockEnvironment environment = validEnvironment();
        environment.setProperty("CC_EXTERNAL_BROKER_ENABLED", "true");
        // CC_BROKER_USERNAME / CC_BROKER_PASSWORD unset (no 'admin' fallback remains)

        IllegalStateException exception = assertThrows(IllegalStateException.class,
                () -> validator.postProcessEnvironment(environment, application));

        assertTrue(exception.getMessage().contains("CC_BROKER_USERNAME"));

        MockEnvironment environment2 = validEnvironment();
        environment2.setProperty("CC_EXTERNAL_BROKER_ENABLED", "true");
        environment2.setProperty("CC_BROKER_USERNAME", "dummy-broker-user");

        IllegalStateException exception2 = assertThrows(IllegalStateException.class,
                () -> validator.postProcessEnvironment(environment2, application));

        assertTrue(exception2.getMessage().contains("CC_BROKER_PASSWORD"));
    }

    @Test
    void externalBrokerWithExplicitCredentialsPasses() {
        MockEnvironment environment = validEnvironment();
        environment.setProperty("CC_EXTERNAL_BROKER_ENABLED", "true");
        environment.setProperty("CC_BROKER_USERNAME", "dummy-broker-user");
        environment.setProperty("CC_BROKER_PASSWORD", "dummy-broker-password");

        assertDoesNotThrow(() -> validator.postProcessEnvironment(environment, application));
    }

    @Test
    void embeddedBrokerDefaultNeedsNoBrokerCredentials() {
        MockEnvironment environment = validEnvironment();
        // CC_EXTERNAL_BROKER_ENABLED unset -> defaults to false (embedded broker)

        assertDoesNotThrow(() -> validator.postProcessEnvironment(environment, application));
    }

    @Test
    void wildcardOriginsFailStartupOutsideTestProfile() {
        MockEnvironment environment = validEnvironment();
        environment.setProperty("CC_ALLOWED_ORIGINS", "*");

        IllegalStateException exception = assertThrows(IllegalStateException.class,
                () -> validator.postProcessEnvironment(environment, application));

        assertTrue(exception.getMessage().contains("CC_ALLOWED_ORIGINS"));
    }

    @Test
    void blankOriginsFailStartupOutsideTestProfile() {
        MockEnvironment environment = validEnvironment();
        environment.setProperty("CC_ALLOWED_ORIGINS", "  ");

        IllegalStateException exception = assertThrows(IllegalStateException.class,
                () -> validator.postProcessEnvironment(environment, application));

        assertTrue(exception.getMessage().contains("CC_ALLOWED_ORIGINS"));
    }

    @Test
    void wildcardOriginsInACommaListAlsoFailStartup() {
        MockEnvironment environment = validEnvironment();
        environment.setProperty("CC_ALLOWED_ORIGINS", "https://clipcascade.example.com, *");

        assertThrows(IllegalStateException.class,
                () -> validator.postProcessEnvironment(environment, application));
    }

    @Test
    void wildcardOriginsAreToleratedOnlyForLocalTestProfiles() {
        MockEnvironment environment = validEnvironment();
        environment.setProperty("CC_ALLOWED_ORIGINS", "*");
        environment.setActiveProfiles("local-test");

        assertDoesNotThrow(() -> validator.postProcessEnvironment(environment, application));

        MockEnvironment environment2 = validEnvironment();
        environment2.setProperty("CC_ALLOWED_ORIGINS", "*");
        environment2.setActiveProfiles("test");

        assertDoesNotThrow(() -> validator.postProcessEnvironment(environment2, application));

        MockEnvironment environment3 = validEnvironment();
        environment3.setProperty("CC_ALLOWED_ORIGINS", "*");
        environment3.setActiveProfiles("production");

        assertThrows(IllegalStateException.class,
                () -> validator.postProcessEnvironment(environment3, application));
    }

    @Test
    void explicitOriginsPassStartup() {
        MockEnvironment environment = validEnvironment();
        environment.setProperty("CC_ALLOWED_ORIGINS", "https://clipcascade.example.com,https://app.example.com");

        assertDoesNotThrow(() -> validator.postProcessEnvironment(environment, application));
    }
}
