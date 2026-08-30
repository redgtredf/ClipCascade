package com.acme.clipcascade.utils;

import java.util.Arrays;
import java.util.List;
import java.util.regex.Pattern;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.security.web.util.matcher.IpAddressMatcher;
import org.springframework.stereotype.Component;
import org.springframework.web.context.request.RequestContextHolder;
import org.springframework.web.context.request.ServletRequestAttributes;

import com.acme.clipcascade.config.ClipCascadeProperties;
import com.acme.clipcascade.constants.IpResolverConstants;

import jakarta.servlet.http.HttpServletRequest;

/**
 * Resolves the real client IP address without trusting client-supplied
 * headers indiscriminately.
 *
 * The previous implementation returned the leftmost entry of any of eleven
 * client-controlled headers. That allowed attackers to rotate fake
 * X-Forwarded-For values to bypass per-IP brute-force limits, or to lock out
 * arbitrary accounts by exhausting the unique-IP lockout.
 *
 * Resolution rules:
 * - The direct socket peer ({@code request.getRemoteAddr()}) is used unless
 *   it belongs to a configured trusted proxy ({@code CC_TRUSTED_PROXIES}).
 * - Behind a trusted proxy, X-Forwarded-For is walked right-to-left and the
 *   first untrusted entry is taken as the client IP. Standard proxies append
 *   the address of the peer they saw, so client-supplied (left-side) entries
 *   can never win.
 * - Malformed or placeholder entries are skipped; if no usable entry is
 *   found, the socket peer is used.
 *
 * NOTE: {@code server.forward-headers-strategy} must stay {@code none}.
 * Spring's "framework" strategy would wrap the request and overwrite
 * {@code getRemoteAddr()} from the (untrusted) forwarded headers before this
 * resolver runs, re-enabling the spoofing this class exists to prevent.
 */
@Component
public class IpAddressResolver {

    private static final Logger logger = (Logger) LoggerFactory
            .getLogger(IpAddressResolver.class);

    // Strict dotted-quad IPv4 (no leading-zero ambiguity beyond single zeros).
    private static final Pattern IPV4_PATTERN = Pattern
            .compile("^(25[0-5]|2[0-4]\\d|1\\d\\d|[1-9]?\\d)(\\.(25[0-5]|2[0-4]\\d|1\\d\\d|[1-9]?\\d)){3}$");

    private static final Pattern IPV6_GROUP_PATTERN = Pattern.compile("^[0-9A-Fa-f]{1,4}$");

    private final List<IpAddressMatcher> trustedProxyMatchers;

    public IpAddressResolver(ClipCascadeProperties clipCascadeProperties) {
        String configured = clipCascadeProperties == null
                ? null
                : clipCascadeProperties.getTrustedProxies();
        if (configured == null || configured.isBlank()) {
            this.trustedProxyMatchers = List.of();
            return;
        }
        this.trustedProxyMatchers = Arrays.stream(configured.split(","))
                .map(String::strip)
                .filter(entry -> !entry.isEmpty())
                .map(entry -> {
                    try {
                        return new IpAddressMatcher(entry);
                    } catch (IllegalArgumentException e) {
                        logger.warn("Ignoring invalid CC_TRUSTED_PROXIES entry '{}': {}", entry, e.getMessage());
                        return null;
                    }
                })
                .filter(matcher -> matcher != null)
                .toList();
    }

    /**
     * Resolves the client IP for the current request context.
     */
    public String getUserIpAddress() {
        Object requestAttributes = RequestContextHolder.getRequestAttributes();
        if (!(requestAttributes instanceof ServletRequestAttributes servletRequestAttributes)) {
            return IpResolverConstants.UNKNOWN_CLIENT;
        }
        return resolveClientIp(servletRequestAttributes.getRequest());
    }

    /**
     * See class javadoc for the resolution rules.
     */
    public String resolveClientIp(HttpServletRequest request) {
        String remoteAddr = request.getRemoteAddr();
        if (remoteAddr == null || remoteAddr.isBlank()) {
            return IpResolverConstants.UNKNOWN_CLIENT;
        }

        if (!isTrustedProxy(remoteAddr)) {
            return normalize(remoteAddr);
        }

        String forwardedFor = request.getHeader(IpResolverConstants.FORWARDED_FOR_HEADER);
        if (forwardedFor == null || forwardedFor.isBlank()) {
            return normalize(remoteAddr);
        }

        String[] entries = forwardedFor.split(",");
        for (int i = entries.length - 1; i >= 0; i--) {
            String candidate = entries[i].strip();
            if (candidate.isEmpty() || IpResolverConstants.UNKNOWN.equalsIgnoreCase(candidate)) {
                continue;
            }
            if (!isValidIpLiteral(candidate) || isTrustedProxy(candidate)) {
                // Not an IP literal (client-controlled garbage) or another
                // trusted proxy hop: keep walking right-to-left.
                continue;
            }
            return normalize(candidate);
        }

        // Every entry was a trusted proxy (or unparseable): fall back to the
        // socket peer rather than trusting any client-supplied value.
        return normalize(remoteAddr);
    }

    private boolean isTrustedProxy(String ip) {
        if (trustedProxyMatchers.isEmpty()) {
            return false;
        }
        // Gate on strict IP literals first so the matcher below never
        // resolves hostnames (DNS) from client-controlled input.
        if (!isValidIpLiteral(ip)) {
            return false;
        }
        for (IpAddressMatcher matcher : trustedProxyMatchers) {
            if (matcher.matches(ip)) {
                return true;
            }
        }
        return false;
    }

    /**
     * Validates strict IP literals (IPv4/IPv6) purely syntactically: no DNS
     * lookup can ever be triggered by client-controlled input.
     */
    static boolean isValidIpLiteral(String value) {
        if (value == null || value.isEmpty() || value.length() > 45) {
            return false;
        }
        if (IPV4_PATTERN.matcher(value).matches()) {
            return true;
        }
        return isValidIpv6(value);
    }

    private static boolean isValidIpv6(String value) {
        // Optional embedded IPv4 at the tail (e.g. "::ffff:192.168.0.1"):
        // replace it with two hex groups, then validate hex groups only.
        int lastColon = value.lastIndexOf(':');
        if (lastColon >= 0 && value.indexOf('.', lastColon) >= 0) {
            String tail = value.substring(lastColon + 1);
            if (!IPV4_PATTERN.matcher(tail).matches()) {
                return false;
            }
            value = value.substring(0, lastColon + 1) + "0:0";
        }

        if (value.contains("::")) {
            if (value.indexOf("::") != value.lastIndexOf("::")) {
                return false; // at most one compression marker
            }
            String[] halves = value.split("::", -1);
            int leftCount = halves[0].isEmpty() ? 0 : halves[0].split(":").length;
            int rightCount = halves[1].isEmpty() ? 0 : halves[1].split(":").length;
            return leftCount + rightCount <= 7
                    && allGroupsValid(halves[0]) && allGroupsValid(halves[1]);
        }

        String[] groups = value.split(":", -1);
        if (groups.length != 8) {
            return false;
        }
        return allGroupsValid(value);
    }

    private static boolean allGroupsValid(String groupsSpec) {
        if (groupsSpec.isEmpty()) {
            return true;
        }
        for (String group : groupsSpec.split(":", -1)) {
            if (!IPV6_GROUP_PATTERN.matcher(group).matches()) {
                return false;
            }
        }
        return true;
    }

    private String normalize(String ip) {
        String lowered = ip.toLowerCase();
        // Strip an IPv6 zone index so the same host on different interfaces
        // shares one brute-force bucket.
        int zoneIndex = lowered.indexOf('%');
        return zoneIndex >= 0 ? lowered.substring(0, zoneIndex) : lowered;
    }
}
