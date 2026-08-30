package com.acme.ClipCascade;

import static org.junit.jupiter.api.Assertions.assertDoesNotThrow;
import static org.junit.jupiter.api.Assertions.assertThrows;

import java.security.Principal;

import org.junit.jupiter.api.Test;
import org.springframework.messaging.Message;
import org.springframework.messaging.MessagingException;
import org.springframework.messaging.simp.stomp.StompCommand;
import org.springframework.messaging.simp.stomp.StompHeaderAccessor;
import org.springframework.messaging.support.MessageBuilder;

import com.acme.clipcascade.config.StompDestinationAuthorizationInterceptor;

public class StompDestinationAuthorizationInterceptorTests {

    private final StompDestinationAuthorizationInterceptor interceptor = new StompDestinationAuthorizationInterceptor();

    // The interceptor never uses the channel; null is safe for these unit tests.
    private static final org.springframework.messaging.MessageChannel CHANNEL = null;

    private static final Principal USER = new Principal() {
        @Override
        public String getName() {
            return "alice";
        }
    };

    private Message<byte[]> frame(StompCommand command, String destination, Principal user) {
        StompHeaderAccessor accessor = StompHeaderAccessor.create(command);
        if (destination != null) {
            accessor.setDestination(destination);
        }
        if (user != null) {
            accessor.setUser(user);
        }
        return MessageBuilder.createMessage(new byte[0], accessor.getMessageHeaders());
    }

    // --- allowed frames ---

    @Test
    void userQueueSubscriptionIsAllowed() {
        assertDoesNotThrow(() -> interceptor.preSend(
                frame(StompCommand.SUBSCRIBE, "/user/queue/cliptext", USER), CHANNEL));
    }

    @Test
    void sendToApplicationDestinationIsAllowed() {
        assertDoesNotThrow(() -> interceptor.preSend(
                frame(StompCommand.SEND, "/app/cliptext", USER), CHANNEL));
    }

    @Test
    void connectIsAllowedWithoutDestinationOrChecks() {
        assertDoesNotThrow(() -> interceptor.preSend(
                frame(StompCommand.CONNECT, null, null), CHANNEL));
    }

    @Test
    void disconnectIsAllowed() {
        assertDoesNotThrow(() -> interceptor.preSend(
                frame(StompCommand.DISCONNECT, null, USER), CHANNEL));
    }

    @Test
    void unsubscribeWithoutDestinationIsAllowed() {
        assertDoesNotThrow(() -> interceptor.preSend(
                frame(StompCommand.UNSUBSCRIBE, null, USER), CHANNEL));
    }

    // --- cross-user / broker-escape subscriptions ---

    @Test
    void directBrokerQueueSubscriptionIsRejected() {
        assertThrows(MessagingException.class, () -> interceptor.preSend(
                frame(StompCommand.SUBSCRIBE, "/queue/**", USER), CHANNEL));
    }

    @Test
    void anyBrokerDestinationSubscriptionIsRejected() {
        assertThrows(MessagingException.class, () -> interceptor.preSend(
                frame(StompCommand.SUBSCRIBE, "/queue/cliptext-user1", USER), CHANNEL));
    }

    @Test
    void topicSubscriptionIsRejected() {
        assertThrows(MessagingException.class, () -> interceptor.preSend(
                frame(StompCommand.SUBSCRIBE, "/topic/all", USER), CHANNEL));
    }

    @Test
    void wildcardInUserQueueSubscriptionIsRejected() {
        assertThrows(MessagingException.class, () -> interceptor.preSend(
                frame(StompCommand.SUBSCRIBE, "/user/queue/*", USER), CHANNEL));
    }

    @Test
    void globStarInUserQueueSubscriptionIsRejected() {
        assertThrows(MessagingException.class, () -> interceptor.preSend(
                frame(StompCommand.SUBSCRIBE, "/user/queue/**", USER), CHANNEL));
    }

    @Test
    void pathTemplateInSubscriptionIsRejected() {
        assertThrows(MessagingException.class, () -> interceptor.preSend(
                frame(StompCommand.SUBSCRIBE, "/user/queue/{name}", USER), CHANNEL));
    }

    @Test
    void bareUserPrefixSubscriptionIsRejected() {
        assertThrows(MessagingException.class, () -> interceptor.preSend(
                frame(StompCommand.SUBSCRIBE, "/user/queue/", USER), CHANNEL));
    }

    @Test
    void userPrefixWithoutQueueSegmentIsRejected() {
        assertThrows(MessagingException.class, () -> interceptor.preSend(
                frame(StompCommand.SUBSCRIBE, "/user/other", USER), CHANNEL));
    }

    @Test
    void subscriptionWithoutPrincipalIsRejected() {
        assertThrows(MessagingException.class, () -> interceptor.preSend(
                frame(StompCommand.SUBSCRIBE, "/user/queue/cliptext", null), CHANNEL));
    }

    // --- send escapes ---

    @Test
    void sendDirectlyToBrokerQueueIsRejected() {
        assertThrows(MessagingException.class, () -> interceptor.preSend(
                frame(StompCommand.SEND, "/queue/cliptext-user1", USER), CHANNEL));
    }

    @Test
    void sendToAnotherUsersQueueIsRejected() {
        assertThrows(MessagingException.class, () -> interceptor.preSend(
                frame(StompCommand.SEND, "/user/bob/queue/cliptext", USER), CHANNEL));
    }

    @Test
    void wildcardSendDestinationIsRejected() {
        assertThrows(MessagingException.class, () -> interceptor.preSend(
                frame(StompCommand.SEND, "/app/*", USER), CHANNEL));
    }

    @Test
    void bareApplicationPrefixSendIsRejected() {
        assertThrows(MessagingException.class, () -> interceptor.preSend(
                frame(StompCommand.SEND, "/app/", USER), CHANNEL));
    }

    @Test
    void sendWithoutDestinationIsRejected() {
        assertThrows(MessagingException.class, () -> interceptor.preSend(
                frame(StompCommand.SEND, null, USER), CHANNEL));
    }

    @Test
    void sendWithoutPrincipalIsRejected() {
        assertThrows(MessagingException.class, () -> interceptor.preSend(
                frame(StompCommand.SEND, "/app/cliptext", null), CHANNEL));
    }
}
