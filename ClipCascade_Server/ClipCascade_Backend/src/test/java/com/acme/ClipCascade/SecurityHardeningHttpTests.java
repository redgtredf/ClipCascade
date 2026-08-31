package com.acme.ClipCascade;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertTrue;

import java.net.CookieManager;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.test.context.ActiveProfiles;
import org.springframework.test.context.TestPropertySource;

import com.acme.clipcascade.constants.RoleConstants;
import com.acme.clipcascade.model.Users;
import com.acme.clipcascade.service.FacadeUserService;
import com.acme.clipcascade.service.UserService;
import com.acme.clipcascade.utils.HashingUtility;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;

/**
 * HTTP-level hardening against the REAL embedded server (real security
 * filter chain, real form login, real sessions): non-admins must get 403
 * (not 200 + empty body) from admin file endpoints, and a self-service
 * password change must prove knowledge of the current password.
 *
 * Dummy values only.
 */
@SpringBootTest(webEnvironment = SpringBootTest.WebEnvironment.RANDOM_PORT)
@ActiveProfiles("test")
@TestPropertySource(properties = {
        "spring.datasource.url=jdbc:h2:mem:security-hardening-test;MODE=PostgreSQL;DB_CLOSE_DELAY=-1",
        // this test talks plain HTTP to the embedded server; the production
        // 'secure' cookie flag (for HTTPS deployments) would make the client
        // refuse the session cookie
        "server.servlet.session.cookie.secure=false"
})
class SecurityHardeningHttpTests {

    private static final String USERNAME = "plain-user";
    private static final String RAW_OLD_PASSWORD = "UserPass12345";
    private static final String RAW_NEW_PASSWORD = "BrandNewPass99";

    @Autowired
    private FacadeUserService facadeUserService;

    @Autowired
    private UserService userService;

    @Value("${CC_ADMIN_USERNAME}")
    private String adminUsername;

    @Value("${local.server.port}")
    private int port;

    private String root() {
        return "http://localhost:" + port;
    }

    private String sha3_512Hex(String raw) throws Exception {
        MessageDigest digest = MessageDigest.getInstance("SHA3-512");
        return HashingUtility.convertBytesToLowercaseHex(
                digest.digest(raw.getBytes(StandardCharsets.UTF_8)));
    }

    @BeforeEach
    void seedNonAdminUser() throws Exception {
        if (!userService.userExists(USERNAME)) {
            facadeUserService.registerUser(new Users(
                    USERNAME,
                    sha3_512Hex(RAW_OLD_PASSWORD),
                    RoleConstants.USER,
                    true));
        }
    }

    private static final class Session {
        final HttpClient client = HttpClient.newBuilder()
                .cookieHandler(new CookieManager())
                .followRedirects(HttpClient.Redirect.NEVER)
                .build();
    }

    private static String firstMatch(String body, String regex) {
        Matcher matcher = Pattern.compile(regex).matcher(body);
        if (!matcher.find()) {
            throw new IllegalStateException("pattern not found: " + regex);
        }
        return matcher.group(1);
    }

    /** Logs in through the real form-login flow (CSRF + session cookie). */
    private Session login(String username, String rawPassword) throws Exception {
        Session session = new Session();

        HttpResponse<String> page = session.client.send(
                HttpRequest.newBuilder(URI.create(root() + "/login")).GET().build(),
                HttpResponse.BodyHandlers.ofString());

        String csrf = firstMatch(page.body(), "name=\"_csrf\"\\s+value=\"([^\"]+)\"");

        String form = "username=" + java.net.URLEncoder.encode(username, StandardCharsets.UTF_8)
                + "&password=" + java.net.URLEncoder.encode(sha3_512Hex(rawPassword), StandardCharsets.UTF_8)
                + "&_csrf=" + java.net.URLEncoder.encode(csrf, StandardCharsets.UTF_8);

        HttpResponse<String> response = session.client.send(
                HttpRequest.newBuilder(URI.create(root() + "/login"))
                        .header("Content-Type", "application/x-www-form-urlencoded")
                        .POST(HttpRequest.BodyPublishers.ofString(form))
                        .build(),
                HttpResponse.BodyHandlers.ofString());

        assertEquals(302, response.statusCode(), "form login must redirect on success");
        assertNotNull(response.headers().firstValue("Location").orElse(null));
        assertFalse(response.headers().firstValue("Location").orElse("").contains("error"),
                "login must not redirect to the error page");
        return session;
    }

    private String csrfToken(Session session) throws Exception {
        HttpResponse<String> response = session.client.send(
                HttpRequest.newBuilder(URI.create(root() + "/csrf-token")).GET().build(),
                HttpResponse.BodyHandlers.ofString());
        JsonNode json = new ObjectMapper().readTree(response.body());
        return json.get("token").asText() + "|" + json.get("headerName").asText();
    }

    private HttpResponse<String> putJson(Session session, String path, String json) throws Exception {
        String[] tokenParts = csrfToken(session).split("\\|");
        return session.client.send(
                HttpRequest.newBuilder(URI.create(root() + path))
                        .header("Content-Type", "application/json")
                        .header(tokenParts[1], tokenParts[0])
                        .PUT(HttpRequest.BodyPublishers.ofString(json))
                        .build(),
                HttpResponse.BodyHandlers.ofString());
    }

    @Test
    void nonAdminGetsForbiddenFromAdminFileEndpoint() throws Exception {
        Session session = login(USERNAME, RAW_OLD_PASSWORD);

        HttpResponse<String> response = session.client.send(
                HttpRequest.newBuilder(URI.create(root() + "/admin/bfa-snapshot-file")).GET().build(),
                HttpResponse.BodyHandlers.ofString());

        assertEquals(403, response.statusCode(),
                "non-admins must get 403, not 200 with an empty body");
        assertEquals(0, response.body().length(), "forbidden response must not leak the tracker");
    }

    @Test
    void adminGetsTheSnapshotFile() throws Exception {
        Session session = login(adminUsername, "DummyAdminPass123");

        HttpResponse<String> response = session.client.send(
                HttpRequest.newBuilder(URI.create(root() + "/admin/bfa-snapshot-file")).GET().build(),
                HttpResponse.BodyHandlers.ofString());

        assertEquals(200, response.statusCode());
        JsonNode json = new ObjectMapper().readTree(response.body());
        assertNotNull(json, "admin must receive the tracker JSON, not an empty body");
    }

    @Test
    void updatePasswordWithoutOldPasswordIsRejected() throws Exception {
        Session session = login(USERNAME, RAW_OLD_PASSWORD);

        String body = "{\"newPassword\": \"" + sha3_512Hex(RAW_NEW_PASSWORD) + "\"}";
        assertEquals(400, putJson(session, "/update-password", body).statusCode());

        assertFalse(userService.verifyPassword(USERNAME, sha3_512Hex(RAW_NEW_PASSWORD)),
                "password must be unchanged when the old password is missing");
    }

    @Test
    void updatePasswordWithWrongOldPasswordIsRejected() throws Exception {
        Session session = login(USERNAME, RAW_OLD_PASSWORD);

        String body = "{\"oldPassword\": \"" + sha3_512Hex("WrongPass0000")
                + "\", \"newPassword\": \"" + sha3_512Hex(RAW_NEW_PASSWORD) + "\"}";
        assertEquals(400, putJson(session, "/update-password", body).statusCode());

        assertFalse(userService.verifyPassword(USERNAME, sha3_512Hex(RAW_NEW_PASSWORD)));
        assertTrue(userService.verifyPassword(USERNAME, sha3_512Hex(RAW_OLD_PASSWORD)),
                "old password must still be valid after a rejected change");
    }

    @Test
    void updatePasswordWithCorrectOldPasswordSucceeds() throws Exception {
        Session session = login(USERNAME, RAW_OLD_PASSWORD);

        // sanity: seed may have been changed by another test run; ensure the
        // old password is the one we think it is
        assertTrue(userService.verifyPassword(USERNAME, sha3_512Hex(RAW_OLD_PASSWORD)));

        String body = "{\"oldPassword\": \"" + sha3_512Hex(RAW_OLD_PASSWORD)
                + "\", \"newPassword\": \"" + sha3_512Hex(RAW_NEW_PASSWORD) + "\"}";
        assertEquals(200, putJson(session, "/update-password", body).statusCode());

        assertTrue(userService.verifyPassword(USERNAME, sha3_512Hex(RAW_NEW_PASSWORD)),
                "new password must be active after a successful change");

        // the NEW password must now work for a real login
        assertNotNull(login(USERNAME, RAW_NEW_PASSWORD));

        // restore so test order never matters
        assertNotNull(facadeUserService.updatePassword(
                USERNAME, sha3_512Hex(RAW_OLD_PASSWORD)));
    }
}
