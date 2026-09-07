"""SSRF guard for operator-supplied Prometheus endpoint URLs.

`is_safe_endpoint_url` is run once at endpoint-registration time; the hot-path
DNS-rebinding re-check (`_cached_is_safe_endpoint_url` / `_filter_safe_candidates`)
lives in prometheus_client.py and calls into here. Extracted verbatim from
app.py — behaviour unchanged.
"""
import ipaddress
import socket
from urllib.parse import urlparse


def _is_blocked_ip(ip):
    return (ip.is_link_local or
            ip.is_multicast or
            ip.is_reserved or
            ip.is_loopback or
            (isinstance(ip, ipaddress.IPv4Address) and str(ip).startswith('169.254.')) or
            (isinstance(ip, ipaddress.IPv6Address) and (ip.is_site_local or ip.ipv4_mapped)))


def is_safe_endpoint_url(url: str):
    try:
        url = url.strip()
        # Reject characters that have no place in a URL and would let a stored
        # endpoint break out of an HTML attribute / element when the endpoint
        # manager renders it (defence in depth alongside client-side escaping).
        if any(c in url for c in '"\'<>`\\ \t\n\r') or any(ord(c) < 0x20 for c in url):
            return False, "Endpoint URL contains illegal characters"
        parsed = urlparse(url)
        if parsed.scheme not in ('http', 'https'):
            return False, "Invalid scheme; only http and https are allowed"
        if not parsed.netloc or '@' in parsed.netloc:
            return False, "Invalid URL format or credentials not allowed"
        hostname = (parsed.hostname or '').lower().strip('[]')
        if not hostname:
            return False, "Host missing in URL"

        blocked_hosts = {
            'metadata.google.internal',
            '169.254.169.254',
            '100.100.100.200',
            'instance-data',
            'fd00:ec2::254',
            'localhost',
        }
        if hostname in blocked_hosts or hostname.startswith("169.254."):
            return False, "Endpoint URL is not allowed (restricted network)"

        try:
            ip = ipaddress.ip_address(hostname)
            if _is_blocked_ip(ip):
                return False, "Endpoint URL is not allowed (restricted network)"
            return True, ""
        except ValueError:
            pass  # hostname is a name, not a literal IP - resolve it below

        # Hostname (not a literal IP): resolve and check every address it maps
        # to, so a name pointed at a loopback/link-local/metadata IP (DNS
        # rebinding, or just a misconfigured record) can't slip past the
        # literal-IP checks above. A hostname that fails to resolve here is
        # NOT rejected — it's likely a Docker Compose service name only
        # resolvable from inside the compose network (e.g. added before that
        # container exists), same as the pre-existing behavior for hostnames.
        try:
            addr_infos = socket.getaddrinfo(hostname, None)
        except socket.gaierror:
            return True, ""
        for family, _, _, _, sockaddr in addr_infos:
            ip = ipaddress.ip_address(sockaddr[0])
            if _is_blocked_ip(ip):
                return False, "Endpoint URL is not allowed (restricted network)"

        return True, ""
    except Exception as e:
        return False, f"Invalid URL: {e}"
