"""
Regression tests for the host-based public-surface gates
(backend/backend/middleware.py).

The Tailscale Funnel serves the API publicly; AdminHostGateMiddleware and
WriteEndpointGateMiddleware hide admin + write/trigger endpoints from public
hosts while leaving the read/query surface open. No DB / network required.
"""

import pytest
from django.http import Http404
from django.test import RequestFactory

from backend.middleware import (
    AdminHostGateMiddleware,
    WriteEndpointGateMiddleware,
    is_public_host,
)

PUBLIC_HOST = 'public.example.com'


@pytest.fixture(autouse=True)
def _fixed_local_hosts(monkeypatch):
    """Pin the trusted-host set so tests don't depend on .env settings."""
    monkeypatch.setattr(
        'backend.middleware._LOCAL_HOSTS',
        {'localhost', '127.0.0.1'},
    )


def _ok_response(request):
    return request


def _call(middleware_cls, path, host):
    rf = RequestFactory()
    request = rf.get(path)
    request.META['HTTP_HOST'] = host
    return middleware_cls(_ok_response)(request)


class TestIsPublicHost:
    def test_local_hosts_are_private(self):
        for host in ('localhost', '127.0.0.1', 'localhost:8000'):
            assert not is_public_host(host), host

    def test_funnel_host_is_public(self):
        assert is_public_host(PUBLIC_HOST)


class TestAdminHostGate:
    def test_admin_404_on_public_host(self):
        with pytest.raises(Http404):
            _call(AdminHostGateMiddleware, '/admin/', PUBLIC_HOST)

    def test_admin_ok_on_local_host(self):
        resp = _call(AdminHostGateMiddleware, '/admin/', 'localhost')
        assert resp is not None

    def test_non_admin_path_unaffected(self):
        resp = _call(AdminHostGateMiddleware, '/api/system/status/', PUBLIC_HOST)
        assert resp is not None


class TestWriteEndpointGate:
    @pytest.mark.parametrize('path', [
        '/api/worldkg-pipeline-v2/start/',
        '/api/country-preprocess/',
        '/api/nca/fingerprint/compute/',
        '/api/nca/drift/compute/',
        '/api/nca/apply-link/',
        '/api/nca/enrich/entity/',
        '/api/igea/align/',
        '/api/geovectors/encode/',
    ])
    def test_write_endpoints_404_on_public_host(self, path):
        with pytest.raises(Http404):
            _call(WriteEndpointGateMiddleware, path, PUBLIC_HOST)

    def test_write_endpoints_ok_on_local_host(self):
        resp = _call(
            WriteEndpointGateMiddleware,
            '/api/worldkg-pipeline-v2/start/',
            'localhost',
        )
        assert resp is not None

    def test_read_endpoints_stay_public(self):
        for path in (
            '/api/system/status/',
            '/api/nca/execute-query/',
            '/api/nca/research/chat/',
            '/api/snapshot-jobs/',
        ):
            assert _call(WriteEndpointGateMiddleware, path, PUBLIC_HOST) is not None
