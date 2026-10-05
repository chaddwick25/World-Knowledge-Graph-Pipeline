"""
Host-based public-surface guards.

The Tailscale Funnel exposes the homeserver publicly with no reverse proxy
in the path (TLS terminates at tailscaled), so there is no Caddy/nginx layer
to hang access control on. These guards enforce, Django-side:

- AdminHostGateMiddleware: /admin serves only local hosts; the public funnel
  host gets a 404 that hides the surface.
- WriteEndpointGateMiddleware: write/trigger endpoints (pipeline start, graph
  mutation, GPU-heavy compute) serve only local hosts; the public funnel host
  gets a 404. Read/query endpoints stay public — that is the product surface.

Local hosts are configurable via TRUSTED_LOCAL_HOSTS (default localhost,
127.0.0.1). Extend it (e.g. with the tailnet IP) for admin/API access from
other trusted devices.
"""

from django.conf import settings
from django.http import Http404

_LOCAL_HOSTS = {
    h.strip().lower()
    for h in getattr(settings, 'TRUSTED_LOCAL_HOSTS', 'localhost,127.0.0.1').split(',')
    if h.strip()
}

# Write/trigger endpoints — launching a pipeline, mutating the graph, or
# burning GPU hours must not be reachable from the public funnel. Same
# host-trust model as the admin gate: public hosts get a 404.
_WRITE_PATHS = frozenset({
    '/api/worldkg-pipeline-v2/start/',
    '/api/country-preprocess/',
    '/api/nca/fingerprint/compute/',
    '/api/nca/drift/compute/',
    '/api/nca/apply-link/',
    '/api/nca/enrich/entity/',
    '/api/igea/align/',
    '/api/geovectors/encode/',
})


def is_public_host(host):
    """True when the Host header is not a trusted local address."""
    return (host or '').split(':')[0].lower() not in _LOCAL_HOSTS


class AdminHostGateMiddleware:
    """404 any /admin* request whose Host is not a local address."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.path == '/admin' or request.path.startswith('/admin/'):
            if is_public_host(request.get_host() or ''):
                raise Http404('Admin is local-only.')
        return self.get_response(request)


class WriteEndpointGateMiddleware:
    """404 any write/trigger endpoint on public (funnel) hosts."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.path in _WRITE_PATHS and is_public_host(request.get_host() or ''):
            raise Http404('Endpoint is local-only.')
        return self.get_response(request)
