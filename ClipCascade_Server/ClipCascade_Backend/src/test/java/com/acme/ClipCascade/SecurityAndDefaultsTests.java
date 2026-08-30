package com.acme.ClipCascade;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

import java.net.HttpURLConnection;
import java.net.URI;
import java.nio.charset.StandardCharsets;

import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.boot.test.web.server.LocalServerPort;
import org.springframework.core.env.Environment;
import org.springframework.test.context.ActiveProfiles;
import org.springframework.test.context.TestPropertySource;

import com.acme.clipcascade.config.ClipCascadeProperties;

/**
 * Network-facing defaults: public /health is minimal, protected routes (incl.
 * the WebSocket endpoints) reject unauthenticated access, restrictive feature
 * defaults, hardened session cookies, forwarded-header awareness.
 */
@SpringBootTest(webEnvironment = SpringBootTest.WebEnvironment.RANDOM_PORT)
@ActiveProfiles("test")
@TestPropertySource(properties = {
        "spring.datasource.url=jdbc:h2:mem:security-test;MODE=PostgreSQL;DB_CLOSE_DELAY=-1"
})
class SecurityAndDefaultsTests {

    @LocalServerPort
    private int port;

    @Autowired
    private ClipCascadeProperties clipCascadeProperties;

    @Autowired
    private Environment environment;

    private HttpURLConnection openConnection(String path, String method) throws Exception {
        HttpURLConnection connection = (HttpURLConnection) URI
                .create("http://localhost:" + port + path).toURL().openConnection();
        connection.setRequestMethod(method);
        connection.setInstanceFollowRedirects(false); // see redirects as-is
        return connection;
    }

    private int statusOf(String path) throws Exception {
        return openConnection(path, "GET").getResponseCode();
    }

    @Test
    void publicHealthEndpointReturnsMinimalStatusOnly() throws Exception {
        HttpURLConnection connection = openConnection("/health", "GET");
        assertEquals(200, connection.getResponseCode());

        String body = new String(connection.getInputStream().readAllBytes(), StandardCharsets.UTF_8);
        assertEquals("OK", body, "/health must expose nothing beyond the status");
    }

    @Test
    void unauthenticatedAccessToProtectedRoutesIsRejected() throws Exception {
        assertRejected(statusOf("/whoami"));
        assertRejected(statusOf("/admin/all-users"));
        assertRejected(statusOf("/validate-session"));

        // WebSocket endpoints are protected by the same security filter chain
        assertRejected(statusOf("/clipsocket"));
        assertRejected(statusOf("/p2psignaling"));
    }

    private void assertRejected(int status) {
        assertTrue(status == 301 || status == 302 || status == 307 || status == 401 || status == 403,
                "expected the request to be rejected (redirect/unauthorized), got HTTP " + status);
    }

    @Test
    void restrictiveDefaultsAreApplied() {
        assertFalse(clipCascadeProperties.getSignupEnabled(), "signup must default to disabled");
        assertFalse(clipCascadeProperties.getP2pEnabled(), "P2P must default to disabled");
        assertFalse(clipCascadeProperties.getExternalBrokerEnabled(), "external broker must default to disabled");
    }

    @Test
    void sessionCookiesAreHardenedAndForwardedHeadersAreNotTrusted() {
        assertEquals("true", environment.getProperty("server.servlet.session.cookie.secure"));
        assertEquals("true", environment.getProperty("server.servlet.session.cookie.http-only"));
        assertEquals("lax", environment.getProperty("server.servlet.session.cookie.same-site"));
        // "framework" would trust client-supplied X-Forwarded-For
        // unconditionally (spoofable); IpAddressResolver + CC_TRUSTED_PROXIES
        // handle forwarded headers instead.
        assertEquals("none", environment.getProperty("server.forward-headers-strategy"));
    }
}
