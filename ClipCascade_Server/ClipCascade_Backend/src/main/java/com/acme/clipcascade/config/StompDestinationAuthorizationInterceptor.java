package com.acme.clipcascade.config;

import java.security.Principal;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.messaging.Message;
import org.springframework.messaging.MessageChannel;
import org.springframework.messaging.MessagingException;
import org.springframework.messaging.simp.stomp.StompCommand;
import org.springframework.messaging.simp.stomp.StompHeaderAccessor;
import org.springframework.messaging.support.ChannelInterceptor;
import org.springframework.messaging.support.MessageHeaderAccessor;

/**
 * Authorizes STOMP frames by destination on the client inbound channel.
 *
 * Without this guard, any authenticated WebSocket client could SUBSCRIBE to
 * broker destinations directly (e.g. "/queue/**") and receive clipboard
 * messages relayed for other users, because isolation normally relies on the
 * "/user/queue/..." prefix being resolved per principal.
 *
 * Allowed frames:
 * - SUBSCRIBE: exactly "/user/queue/<segment>" for the authenticated user
 *   (no wildcard/path-pattern characters). Spring resolves the subscription
 *   to the subscribing principal's own session queue.
 * - SEND: exactly "/app/<segment...>" (application-level handler only).
 * - CONNECT / DISCONNECT / HEARTBEAT / UNSUBSCRIBE / ACK / NACK: pass
 *   through unchanged.
 *
 * Everything else is rejected with a MessagingException, which the STOMP
 * layer converts into an ERROR frame for the offending client.
 */
public class StompDestinationAuthorizationInterceptor implements ChannelInterceptor {

    private static final Logger logger = (Logger) LoggerFactory
            .getLogger(StompDestinationAuthorizationInterceptor.class);

    private static final String USER_QUEUE_PREFIX = "/user/queue/";
    private static final String APPLICATION_PREFIX = "/app/";

    @Override
    public Message<?> preSend(Message<?> message, MessageChannel channel) {
        StompHeaderAccessor accessor = MessageHeaderAccessor.getAccessor(message, StompHeaderAccessor.class);

        if (accessor == null || accessor.getCommand() == null) {
            // Not a STOMP frame (e.g. heartbeats); nothing to authorize.
            return message;
        }

        StompCommand command = accessor.getCommand();
        if (command == StompCommand.SUBSCRIBE) {
            authorizeSubscription(accessor);
        } else if (command == StompCommand.SEND) {
            authorizeSend(accessor);
        }

        return message;
    }

    private void authorizeSubscription(StompHeaderAccessor accessor) {
        String username = requirePrincipal(accessor);
        String destination = requireDestination(accessor);

        if (!destination.startsWith(USER_QUEUE_PREFIX)) {
            reject(accessor, username, destination,
                    "Subscriptions are only allowed on '" + USER_QUEUE_PREFIX + "<name>' destinations");
        }

        String name = destination.substring(USER_QUEUE_PREFIX.length());
        if (name.isEmpty()) {
            reject(accessor, username, destination, "Subscription destination must include a queue name");
        }

        if (containsPathPattern(destination)) {
            reject(accessor, username, destination, "Wildcards are not allowed in subscription destinations");
        }
    }

    private void authorizeSend(StompHeaderAccessor accessor) {
        String username = requirePrincipal(accessor);
        String destination = requireDestination(accessor);

        if (!destination.startsWith(APPLICATION_PREFIX)) {
            reject(accessor, username, destination,
                    "Messages may only be sent to '" + APPLICATION_PREFIX + "<name>' destinations");
        }

        String name = destination.substring(APPLICATION_PREFIX.length());
        if (name.isEmpty()) {
            reject(accessor, username, destination, "Send destination must include an endpoint name");
        }

        if (containsPathPattern(destination)) {
            reject(accessor, username, destination, "Wildcards are not allowed in send destinations");
        }
    }

    private String requirePrincipal(StompHeaderAccessor accessor) {
        Principal user = accessor.getUser();
        if (user == null || user.getName() == null || user.getName().isBlank()) {
            throw new MessagingException("Unauthenticated STOMP frame rejected");
        }
        return user.getName();
    }

    private String requireDestination(StompHeaderAccessor accessor) {
        String destination = accessor.getDestination();
        if (destination == null || destination.isBlank()) {
            throw new MessagingException("STOMP frame rejected: missing destination");
        }
        return destination;
    }

    /**
     * The broker registry matches destinations with AntPathMatcher, so '*', '?'
     * and brace templates in a client-supplied destination could widen a
     * subscription beyond its literal name.
     */
    private boolean containsPathPattern(String destination) {
        return destination.indexOf('*') >= 0
                || destination.indexOf('?') >= 0
                || destination.indexOf('{') >= 0;
    }

    private void reject(StompHeaderAccessor accessor, String username, String destination, String reason) {
        logger.warn("Rejected STOMP {} from user '{}' to destination '{}': {}",
                accessor.getMessageType(), username, destination, reason);
        throw new MessagingException(reason);
    }
}
