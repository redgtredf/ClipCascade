package com.acme.ClipCascade;

import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.ArgumentMatchers.argThat;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.times;
import static org.mockito.Mockito.verify;

import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.messaging.MessagingException;
import org.springframework.messaging.simp.SimpMessagingTemplate;
import org.springframework.security.authentication.UsernamePasswordAuthenticationToken;
import org.springframework.security.core.Authentication;
import org.springframework.test.context.ActiveProfiles;
import org.springframework.test.context.TestPropertySource;
import org.springframework.test.context.bean.override.mockito.MockitoBean;

import com.acme.clipcascade.config.ClipCascadeProperties;
import com.acme.clipcascade.constants.RoleConstants;
import com.acme.clipcascade.controller.ClipCascadeController;
import com.acme.clipcascade.model.ClipboardData;
import com.acme.clipcascade.model.UserPrincipal;
import com.acme.clipcascade.model.Users;
import com.acme.clipcascade.service.BruteForceProtectionService;

/**
 * Relay behaviour: messages are relayed ONLY to the sending user's
 * username-scoped destination, and invalid/oversized messages are rejected
 * before relay. Payloads used are dummy strings.
 */
@SpringBootTest(webEnvironment = SpringBootTest.WebEnvironment.RANDOM_PORT)
@ActiveProfiles("test")
@TestPropertySource(properties = {
        "spring.datasource.url=jdbc:h2:mem:relay-test;MODE=PostgreSQL;DB_CLOSE_DELAY=-1"
})
class RelayValidationAndIsolationTests {

    private static final String DESTINATION = "/queue/cliptext";

    @Autowired
    private ClipCascadeController controller;

    @Autowired
    private ClipCascadeProperties clipCascadeProperties;

    @Autowired
    private BruteForceProtectionService bruteForceProtectionService;

    @MockitoBean
    private SimpMessagingTemplate simpMessagingTemplate;

    private Authentication principal(String username) {
        UserPrincipal userPrincipal = new UserPrincipal(
                new Users(username, "dummy-password", RoleConstants.USER, true),
                bruteForceProtectionService);
        return new UsernamePasswordAuthenticationToken(
                userPrincipal, null, userPrincipal.getAuthorities());
    }

    @Test
    void messagesAreRelayedOnlyToTheSendingUser() {
        controller.sendPrivateMessage(
                principal("userA"),
                new ClipboardData("dummy-payload-A", "text", null));

        controller.sendPrivateMessage(
                principal("userB"),
                new ClipboardData("dummy-payload-B", "image", null));

        // Exactly one relay event per user, always to the sender's own
        // username-scoped destination, never to another user's.
        verify(simpMessagingTemplate, times(1)).convertAndSendToUser(
                eq("userA"),
                eq(DESTINATION),
                argThat(message -> message instanceof ClipboardData
                        && "dummy-payload-A".equals(((ClipboardData) message).getPayload())));

        verify(simpMessagingTemplate, times(1)).convertAndSendToUser(
                eq("userB"),
                eq(DESTINATION),
                argThat(message -> message instanceof ClipboardData
                        && "dummy-payload-B".equals(((ClipboardData) message).getPayload())));

        // No other relay happened at all (total = exactly the two above)
        verify(simpMessagingTemplate, times(2)).convertAndSendToUser(anyString(), anyString(), any());
    }

    @Test
    void messageWithUnknownTypeIsRejectedBeforeRelay() {
        assertThrows(MessagingException.class,
                () -> controller.sendPrivateMessage(
                        principal("userA"),
                        new ClipboardData("dummy-payload", "executable", null)));

        verify(simpMessagingTemplate, never()).convertAndSendToUser(anyString(), anyString(), any());
    }

    @Test
    void oversizedMessageIsRejectedBeforeRelay() {
        long maxBytes = clipCascadeProperties.getMaxMessageSizeInBytes();
        String oversizedDummyPayload = "a".repeat((int) maxBytes + 1);

        assertThrows(MessagingException.class,
                () -> controller.sendPrivateMessage(
                        principal("userA"),
                        new ClipboardData(oversizedDummyPayload, "text", null)));

        verify(simpMessagingTemplate, never()).convertAndSendToUser(anyString(), anyString(), any());
    }

    @Test
    void messageWithoutTypeDefaultsToTextAndIsRelayed() {
        controller.sendPrivateMessage(
                principal("userA"),
                new ClipboardData("dummy-payload", null, null));

        verify(simpMessagingTemplate).convertAndSendToUser(
                eq("userA"),
                eq(DESTINATION),
                argThat(message -> message instanceof ClipboardData
                        && "text".equals(((ClipboardData) message).getType())));
    }

    @Test
    void filesTypeFromMobileClientsRemainsRelayable() {
        controller.sendPrivateMessage(
                principal("userA"),
                new ClipboardData("dummy-payload", "files", null));

        verify(simpMessagingTemplate).convertAndSendToUser(
                eq("userA"),
                eq(DESTINATION),
                any(ClipboardData.class));
    }
}
