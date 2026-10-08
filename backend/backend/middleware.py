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
from django.http import Http404, JsonResponse
import hmac

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

# Prefix-gated surfaces: not part of the public product surface (the SPA
# never calls them). Geodata is an operator CRUD registry (datasets /
# records / sources / quality + sync_metadata trigger); IGEA triplets are
# POST model-inference endpoints (predict / score / validate).
_WRITE_PREFIXES = frozenset({
    '/api/geodata/',
    '/api/igea/triplets/',
})


def _is_write_path(path):
    return path in _WRITE_PATHS or any(
        path.startswith(p) for p in _WRITE_PREFIXES
    )


def is_public_host(host):
    """True when the Host header is not a trusted local address."""
    return (host or '').split(':')[0].lower() not in _LOCAL_HOSTS


# Tailnet operator surface: nginx tags requests arriving on the loopback
# :8081 listener (reached only via `tailscale serve` or a local process)
# with X-Trusted-Surface: tailnet. The PUBLIC listener strips the header,
# so a client cannot forge it from the funnel. See deploy/nginx.conf.
_TRUSTED_SURFACE_HEADER = 'X-Trusted-Surface'
_TRUSTED_SURFACE_VALUE = 'tailnet'


def _is_trusted_request(request):
    """Trusted when the Host is local OR the request arrived on the
    tailnet operator surface (nginx-tagged, loopback-only)."""
    if request.headers.get(_TRUSTED_SURFACE_HEADER) == _TRUSTED_SURFACE_VALUE:
        return True
    return not is_public_host(request.get_host() or '')


class AdminHostGateMiddleware:
    """Serve /admin only to trusted hosts (local or the tailnet surface)."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.path == '/admin' or request.path.startswith('/admin/'):
            if not _is_trusted_request(request):
                raise Http404('Admin is local-only.')
        return self.get_response(request)


class WriteEndpointGateMiddleware:
    """404 write/trigger endpoints on public (funnel) hosts; on trusted hosts
    require the operator pipeline key when one is configured.

    The host-trust model alone is spoofable: nginx forwards any Host header
    (server_name _), so a LAN client can fake ``localhost`` and reach the
    write surface. ``X-Pipeline-Key`` must equal
    ``settings.PIPELINE_TRIGGER_KEY`` (env-only, never in the browser bundle)
    on trusted hosts; the public funnel still gets a 404 first.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if _is_write_path(request.path):
            if not _is_trusted_request(request):
                raise Http404('Endpoint is local-only.')
            expected = getattr(settings, 'PIPELINE_TRIGGER_KEY', '')
            if expected and not hmac.compare_digest(
                request.headers.get('X-Pipeline-Key', ''), expected
            ):
                return JsonResponse({'detail': 'Pipeline key required.'}, status=403)
        return self.get_response(request)
