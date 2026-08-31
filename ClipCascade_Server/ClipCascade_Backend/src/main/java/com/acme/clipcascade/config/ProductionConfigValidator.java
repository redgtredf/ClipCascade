package com.acme.clipcascade.config;

import java.util.Arrays;
import java.util.Locale;
import java.util.Set;
import java.util.stream.Collectors;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.boot.SpringApplication;
import org.springframework.boot.env.EnvironmentPostProcessor;
import org.springframework.core.Ordered;
import org.springframework.core.env.ConfigurableEnvironment;
import org.springframework.core.env.Environment;

/**
 * Fail-fast validation of mandatory production configuration.
 *
 * Runs during environment preparation (before any bean or datasource is
 * created), so a misconfigured server refuses to start with a clear error
 * instead of falling back to insecure defaults:
 *
 * - CC_SERVER_DB_PASSWORD: required, no usable default.
 * - CC_ADMIN_USERNAME / CC_ADMIN_PASSWORD: required, no default admin123.
 * - CC_BROKER_USERNAME / CC_BROKER_PASSWORD: required when
 *   CC_EXTERNAL_BROKER_ENABLED=true (embedded broker needs nothing).
 * - CC_ALLOWED_ORIGINS: must be explicit non-wildcard origins, except when a
 *   named local/test profile ('local-test' or 'test') is active.
 */
public class ProductionConfigValidator implements EnvironmentPostProcessor, Ordered {

    private final Logger logger = (Logger) LoggerFactory.getLogger(ProductionConfigValidator.class);

    // Profiles in which wildcard allowed origins are tolerated (local/test only).
    private static final Set<String> WILDCARD_ORIGINS_ALLOWED_PROFILES = Set.of("local-test", "test");

    @Override
    public int getOrder() {
        // Run after config data (application.properties / profiles) is loaded.
        return Ordered.LOWEST_PRECEDENCE;
    }

    @Override
    public void postProcessEnvironment(ConfigurableEnvironment environment, SpringApplication application) {
        requireNonBlank(environment, "CC_SERVER_DB_PASSWORD");

        requireNonBlank(environment, "CC_ADMIN_USERNAME");
        requireNonBlank(environment, "CC_ADMIN_PASSWORD");
        requireMinLength(environment, "CC_ADMIN_PASSWORD", 8);

        if (isExternalBrokerEnabled(environment)) {
            requireNonBlank(environment, "CC_BROKER_USERNAME");
            requireNonBlank(environment, "CC_BROKER_PASSWORD");
        }

        validateAllowedOrigins(environment);

        logger.info("Production configuration validation passed");
    }

    private boolean isExternalBrokerEnabled(Environment environment) {
        String value = environment.getProperty("CC_EXTERNAL_BROKER_ENABLED");
        return value != null && value.strip().equalsIgnoreCase("true");
    }

    private void requireNonBlank(Environment environment, String name) {
        String value = environment.getProperty(name);
        if (value == null || value.isBlank()) {
            throw new IllegalStateException(
                    "Missing required environment variable '" + name
                            + "'. Refusing to start with an insecure default; set it explicitly before starting the server.");
        }
    }

    private void requireMinLength(Environment environment, String name, int minLength) {
        String value = environment.getProperty(name);
        if (value != null && !value.isBlank() && value.length() < minLength) {
            throw new IllegalStateException(
                    "Environment variable '" + name + "' must be at least "
                            + minLength + " characters long. Refusing to start with a weak credential.");
        }
    }

    private void validateAllowedOrigins(Environment environment) {
        String value = environment.getProperty("CC_ALLOWED_ORIGINS");

        boolean blank = value == null || value.strip().isEmpty();
        boolean wildcard = !blank && Arrays.stream(value.split(","))
                .map(origin -> origin.strip())
                .anyMatch(origin -> origin.equals("*"));

        if (!blank && !wildcard) {
            return;
        }

        Set<String> activeProfiles = Arrays.stream(environment.getActiveProfiles())
                .map(profile -> profile.toLowerCase(Locale.ROOT))
                .collect(Collectors.toSet());

        if (activeProfiles.stream().anyMatch(WILDCARD_ORIGINS_ALLOWED_PROFILES::contains)) {
            return;
        }

        throw new IllegalStateException(
                (blank ? "CC_ALLOWED_ORIGINS is not set or blank"
                        : "CC_ALLOWED_ORIGINS contains a wildcard ('*')")
                        + ". Refusing to start: production requires explicit non-wildcard origins. "
                        + "Wildcard origins are only permitted with the 'local-test' (or 'test') profile active.");
    }
}
