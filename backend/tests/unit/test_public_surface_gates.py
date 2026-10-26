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


def _call(middleware_cls, path, host, key=None, surface=None):
    rf = RequestFactory()
    kwargs = {'HTTP_HOST': host}
    if key is not None:
        kwargs['HTTP_X_PIPELINE_KEY'] = key
    if surface is not None:
        kwargs['HTTP_X_TRUSTED_SURFACE'] = surface
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
        '/api/country-preprocess/',
        '/api/nca/fingerprint/compute/',
        '/api/nca/drift/compute/',
        '/api/nca/apply-link/',
        '/api/nca/enrich/entity/',
        '/api/igea/align/',
        '/api/geovectors/encode/',
        # Prefix-gated surfaces (IGEA model-inference endpoints; the
        # geodata DRF surface was retired — its CRUD lives in admin only)
        '/api/igea/triplets/predict/',
        '/api/igea/triplets/score/',
        '/api/igea/triplets/validate/',
    ])
    def test_write_endpoints_404_on_public_host(self, path):
        with pytest.raises(Http404):
            _call(WriteEndpointGateMiddleware, path, PUBLIC_HOST)

    # NOTE: /api/worldkg-pipeline-v2/start/ is intentionally NOT in the 404
    # list above — it is key-gated on every surface (see
    # test_pipeline_trigger_fail_closed_without_key below).

    def test_pipeline_trigger_fail_closed_without_key(self):
        """Fail-closed: with no PIPELINE_TRIGGER_KEY configured (this
        fixture), the key-gated trigger is DENIED on public hosts (403,
        not pass-through) — an unset key must not leave the funnel's
        only gated path triggerable by anyone."""
        resp = _call(
            WriteEndpointGateMiddleware,
            '/api/worldkg-pipeline-v2/start/',
            PUBLIC_HOST,
        )
        assert resp.status_code == 403

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

    def test_public_host_without_key_403(self):
        """Key-gated trigger: public host + no key → 403 (modal appears)."""
        resp = _call(
            WriteEndpointGateMiddleware, '/api/worldkg-pipeline-v2/start/', PUBLIC_HOST,
        )
        assert resp.status_code == 403

    def test_public_host_wrong_key_403(self):
        resp = _call(
            WriteEndpointGateMiddleware, '/api/worldkg-pipeline-v2/start/', PUBLIC_HOST,
            key='wrong',
        )
        assert resp.status_code == 403

    def test_public_host_correct_key_passes(self):
        """The trigger is key-gated (not host-hidden) so the operator can run
        from the funnel with the correct key; the key is the credential."""
        resp = _call(
            WriteEndpointGateMiddleware, '/api/worldkg-pipeline-v2/start/', PUBLIC_HOST,
            key='test-secret',
        )
        assert resp is not None

    def test_public_host_correct_key_still_blocks_other_writes(self):
        """Other write endpoints remain hard-404 on public hosts even with a
        valid key — only the key-gated trigger is funnel-reachable."""
        with pytest.raises(Http404):
            _call(
                WriteEndpointGateMiddleware, '/api/country-preprocess/', PUBLIC_HOST,
                key='test-secret',
            )


class TestTailnetSurface:
    """The loopback operator surface (nginx :8081 via `tailscale serve`) is
    tagged X-Trusted-Surface: tailnet — admin and writes are allowed there
    (writes still need the pipeline key). The public edge strips the header,
    so it cannot be forged from the funnel."""

    @pytest.fixture(autouse=True)
    def _key(self, monkeypatch):
        # Pin the key so these tests don't depend on the ambient .env value.
        monkeypatch.setattr('django.conf.settings.PIPELINE_TRIGGER_KEY', 'test-secret')

    def test_admin_allowed_on_tailnet_surface(self):
        resp = _call(
            AdminHostGateMiddleware, '/admin/', PUBLIC_HOST, surface='tailnet',
        )
        assert resp is not None

    def test_admin_404_without_surface(self):
        with pytest.raises(Http404):
            _call(AdminHostGateMiddleware, '/admin/', PUBLIC_HOST)

    def test_write_requires_key_even_on_tailnet(self):
        resp = _call(
            WriteEndpointGateMiddleware, '/api/worldkg-pipeline-v2/start/', PUBLIC_HOST,
            surface='tailnet',
        )
        assert resp.status_code == 403

    def test_write_with_key_on_tailnet(self):
        resp = _call(
            WriteEndpointGateMiddleware, '/api/worldkg-pipeline-v2/start/', PUBLIC_HOST,
            key='test-secret', surface='tailnet',
        )
        assert resp is not None

    def test_wrong_surface_value_is_public(self):
        """A forged/unknown surface marker gains no trust: non-key-gated write
        endpoints stay 404 on public hosts (the trigger itself is key-gated
        and ignores the surface marker)."""
        with pytest.raises(Http404):
            _call(
                WriteEndpointGateMiddleware, '/api/country-preprocess/', PUBLIC_HOST,
                surface='attacker',
            )
