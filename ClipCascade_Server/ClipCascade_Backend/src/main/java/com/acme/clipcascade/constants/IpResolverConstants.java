package com.acme.clipcascade.constants;

public class IpResolverConstants {

    /**
     * The only forwarded header considered when resolving the client IP.
     * Standard reverse proxies append the address of the peer they saw to
     * this header, so the trusted-proxy walk (see IpAddressResolver) can
     * always discard client-supplied entries on the left.
     */
    public static final String FORWARDED_FOR_HEADER = "X-Forwarded-For";

    // Placeholder sometimes emitted by proxies for unknown clients.
    public static final String UNKNOWN = "unknown";

    // Fallback when no request context is available.
    public static final String UNKNOWN_CLIENT = "0.0.0.0";

    private IpResolverConstants() {
        // private constructor to prevent instantiation
    }
}
