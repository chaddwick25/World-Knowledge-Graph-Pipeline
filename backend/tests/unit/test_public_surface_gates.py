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
    """Pin the trusted-host set AND Django's ALLOWED_HOSTS so tests don't
    depend on .env settings (get_host() validates against ALLOWED_HOSTS)."""
    monkeypatch.setattr(
        'backend.middleware._LOCAL_HOSTS',
        {'localhost', '127.0.0.1'},
    )
    monkeypatch.setattr(
        'django.conf.settings.ALLOWED_HOSTS',
        ['localhost', '127.0.0.1', PUBLIC_HOST],
    )


def _ok_response(request):
    return request


def _call(middleware_cls, path, host, key=None):
    rf = RequestFactory()
    kwargs = {'HTTP_HOST': host}
    if key is not None:
        kwargs['HTTP_X_PIPELINE_KEY'] = key
    request = rf.get(path, **kwargs)
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
        # Prefix-gated surfaces (operator CRUD registry + IGEA inference)
        '/api/geodata/datasets/',
        '/api/geodata/datasets/sync_metadata/',
        '/api/geodata/records/',
        '/api/geodata/sources/',
        '/api/geodata/quality/',
        '/api/igea/triplets/predict/',
        '/api/igea/triplets/score/',
        '/api/igea/triplets/validate/',
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


class TestPipelineTriggerKey:
    """Write endpoints on trusted hosts require X-Pipeline-Key when
    PIPELINE_TRIGGER_KEY is configured (closes the Host-spoofing hole:
    nginx forwards any Host header, so a LAN client can fake localhost)."""

    @pytest.fixture(autouse=True)
    def _key(self, monkeypatch):
        monkeypatch.setattr('django.conf.settings.PIPELINE_TRIGGER_KEY', 'test-secret')

    def test_local_write_without_key(self):
        resp = _call(
            WriteEndpointGateMiddleware, '/api/worldkg-pipeline-v2/start/', 'localhost',
        )
        assert resp.status_code == 403

    def test_local_write_wrong_key(self):
        resp = _call(
            WriteEndpointGateMiddleware, '/api/worldkg-pipeline-v2/start/', 'localhost',
            key='wrong',
        )
        assert resp.status_code == 403

    def test_local_write_correct_key(self):
        resp = _call(
            WriteEndpointGateMiddleware, '/api/worldkg-pipeline-v2/start/', 'localhost',
            key='test-secret',
        )
        assert resp is not None  # passes through to the view

    def test_no_key_configured_allows_local(self, monkeypatch):
        monkeypatch.setattr('django.conf.settings.PIPELINE_TRIGGER_KEY', '')
        resp = _call(WriteEndpointGateMiddleware, '/api/worldkg-pipeline-v2/start/', 'localhost')
        assert resp is not None

    def test_public_host_404_even_with_key(self):
        with pytest.raises(Http404):
            _call(
                WriteEndpointGateMiddleware, '/api/worldkg-pipeline-v2/start/', PUBLIC_HOST,
                key='test-secret',
            )
