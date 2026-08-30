package com.acme.ClipCascade;

import static org.junit.jupiter.api.Assertions.assertEquals;

import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.Test;
import org.springframework.mock.web.MockHttpServletRequest;
import org.springframework.test.util.ReflectionTestUtils;
import org.springframework.web.context.request.RequestContextHolder;
import org.springframework.web.context.request.ServletRequestAttributes;

import com.acme.clipcascade.config.ClipCascadeProperties;
import com.acme.clipcascade.utils.IpAddressResolver;

/**
 * Proves client IP resolution cannot be steered by client-supplied
 * X-Forwarded-For values unless they arrive via a trusted proxy (default:
 * loopback + private ranges), protecting the brute-force buckets from both
 * bypass (rotation) and lockout abuse.
 */
class IpAddressResolverTests {

    // Mirrors the @Value default injected by Spring in production.
    private static final String DEFAULT_TRUSTED_PROXIES =
            "127.0.0.0/8,::1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16";

    private IpAddressResolver resolverWithTrustedProxies(String csv) {
        ClipCascadeProperties properties = new ClipCascadeProperties();
        ReflectionTestUtils.setField(properties, "trustedProxies",
                csv != null ? csv : DEFAULT_TRUSTED_PROXIES);
        return new IpAddressResolver(properties);
    }

    private MockHttpServletRequest request(String remoteAddr, String forwardedFor) {
        MockHttpServletRequest request = new MockHttpServletRequest();
        request.setRemoteAddr(remoteAddr);
        if (forwardedFor != null) {
            request.addHeader("X-Forwarded-For", forwardedFor);
        }
        return request;
    }

    @AfterEach
    void resetRequestContext() {
        RequestContextHolder.resetRequestAttributes();
    }

    // --- direct connections: forwarded headers must be ignored entirely ---

    @Test
    void directConnectionFromUntrustedPeerIgnoresSpoofedForwardedFor() {
        IpAddressResolver resolver = resolverWithTrustedProxies(null);
        assertEquals("203.0.113.50",
                resolver.resolveClientIp(request("203.0.113.50", "6.6.6.6, 7.7.7.7")));
    }

    @Test
    void directConnectionWithoutHeaderUsesSocketPeer() {
        IpAddressResolver resolver = resolverWithTrustedProxies(null);
        assertEquals("203.0.113.50",
                resolver.resolveClientIp(request("203.0.113.50", null)));
    }

    // --- behind a trusted proxy: right-to-left walk, first untrusted wins ---

    @Test
    void spoofedEntriesBehindTrustedProxyAreDiscarded() {
        IpAddressResolver resolver = resolverWithTrustedProxies(null);
        // Proxy appended the real client ("198.51.100.7"); the left-side
        // entries were supplied by the client itself.
        assertEquals("198.51.100.7",
                resolver.resolveClientIp(request("127.0.0.1", "6.6.6.6, 198.51.100.7")));
    }

    @Test
    void rotatedSpoofEntriesNeverChangeTheResolvedIp() {
        IpAddressResolver resolver = resolverWithTrustedProxies(null);
        for (int i = 0; i < 25; i++) {
            String spoof = "10.1.2." + i + ", 192.168.5." + i + ", 198.51.100.7";
            assertEquals("198.51.100.7",
                    resolver.resolveClientIp(request("127.0.0.1", spoof)));
        }
    }

    @Test
    void privateRangeProxyIsTrustedByDefault() {
        IpAddressResolver resolver = resolverWithTrustedProxies(null);
        // Docker/compose-style proxy address.
        assertEquals("198.51.100.7",
                resolver.resolveClientIp(request("172.20.0.9", "198.51.100.7")));
    }

    @Test
    void ipv6LoopbackSocketPeerIsTrusted() {
        IpAddressResolver resolver = resolverWithTrustedProxies(null);
        // Java reports IPv6 loopback in verbose form; the "::1" matcher must
        // still match it.
        assertEquals("198.51.100.7",
                resolver.resolveClientIp(request("0:0:0:0:0:0:0:1", "198.51.100.7")));
    }

    // --- hostile/degenerate header values ---

    @Test
    void garbageAndPlaceholderEntriesAreSkippedWithoutDnsLookups() {
        IpAddressResolver resolver = resolverWithTrustedProxies(null);
        assertEquals("198.51.100.7",
                resolver.resolveClientIp(request("127.0.0.1",
                        "not-an-ip, evil.example.com, unknown, 198.51.100.7")));
    }

    @Test
    void allTrustedChainFallsBackToSocketPeer() {
        IpAddressResolver resolver = resolverWithTrustedProxies(null);
        // Every entry is inside the default trusted ranges: none may be used.
        assertEquals("127.0.0.1",
                resolver.resolveClientIp(request("127.0.0.1", "10.0.0.5, 192.168.1.9")));
    }

    @Test
    void blankHeaderFallsBackToSocketPeer() {
        IpAddressResolver resolver = resolverWithTrustedProxies(null);
        assertEquals("127.0.0.1",
                resolver.resolveClientIp(request("127.0.0.1", "   ")));
    }

    @Test
    void trustedProxyIpsAreExcludedAsClientAddresses() {
        IpAddressResolver resolver = resolverWithTrustedProxies(null);
        // The proxy's own address appears as the only XFF entry (internal hop):
        // trusting it would collapse every user into one brute-force bucket.
        assertEquals("127.0.0.1",
                resolver.resolveClientIp(request("127.0.0.1", "127.0.0.1")));
    }

    // --- configuration variants ---

    @Test
    void explicitPublicProxyCanBeTrusted() {
        IpAddressResolver resolver = resolverWithTrustedProxies("198.51.100.1");
        assertEquals("198.51.100.7",
                resolver.resolveClientIp(request("198.51.100.1", "198.51.100.7")));
    }

    @Test
    void emptyTrustedProxiesMeansDirectOnlyResolution() {
        IpAddressResolver resolver = resolverWithTrustedProxies("");
        assertEquals("127.0.0.1",
                resolver.resolveClientIp(request("127.0.0.1", "198.51.100.7")));
    }

    @Test
    void invalidConfigEntriesAreIgnoredAndValidOnesKept() {
        IpAddressResolver resolver = resolverWithTrustedProxies("not-a-cidr, 127.0.0.0/8");
        assertEquals("198.51.100.7",
                resolver.resolveClientIp(request("127.0.0.1", "198.51.100.7")));
    }

    // --- request-context-less fallback (scheduled tasks etc.) ---

    @Test
    void noRequestContextYieldsUnknownClient() {
        IpAddressResolver resolver = resolverWithTrustedProxies(null);
        assertEquals("0.0.0.0", resolver.getUserIpAddress());
    }

    @Test
    void userIpAddressResolvesThroughRequestContext() {
        RequestContextHolder.setRequestAttributes(
                new ServletRequestAttributes(request("127.0.0.1", "6.6.6.6, 198.51.100.7")));
        IpAddressResolver resolver = resolverWithTrustedProxies(null);
        assertEquals("198.51.100.7", resolver.getUserIpAddress());
    }
}
