"""
Regression tests for the nginx-layer trust invariants (deploy/nginx.conf).

The Django middleware gates (test_public_surface_gates.py) trust the Host
header for local hosts; nginx is the layer that must keep client-controlled
Hosts from ever reaching that branch. The shared host allowlist used to
include localhost/127.0.0.1 on the PUBLIC edge, so a spoofed
"Host: localhost" flipped is_public_host() to "trusted" — the one real
finding of the 2026-10-05 security-review model bake-off
(docs/Schematics/08_Agent_MCP_LLM/07_Security_Review_Models.md, fixed the
same day by splitting the allowlist per surface). These tests parse the
config file so the invariants survive future edits. No DB / network
required.
"""

import re
from pathlib import Path

import pytest

NGINX_CONF = Path(__file__).resolve().parents[3] / 'deploy' / 'nginx.conf'
FUNNEL_HOST = 'thanos.tail560528.ts.net'
LOOPBACK_HOSTS = {'localhost', '127.0.0.1', '100.67.88.104'}


def _extract_block(text, header_pattern):
    """Return the {...} block following header_pattern, brace-matched
    (locations nest inside server blocks, so a non-greedy regex will not
    do). None when the header is absent."""
    m = re.search(header_pattern, text)
    if m is None:
        return None
    open_idx = text.index('{', m.end())
    depth = 0
    for i in range(open_idx, len(text)):
        if text[i] == '{':
            depth += 1
        elif text[i] == '}':
            depth -= 1
            if depth == 0:
                return text[open_idx:i + 1]
    return None


def _map_entries(map_block):
    """Key set of a map block's non-default entries."""
    assert map_block is not None, 'map block missing from nginx.conf'
    return {
        key for key, value in re.findall(
            r'^\s+(\S+)\s+(\d+);$', map_block, re.MULTILINE
        ) if key != 'default'
    }


def _server_block(text, listen_port):
    for m in re.finditer(r'^server \{', text, re.MULTILINE):
        block = _extract_block(text[m.start():], r'server')
        if f'listen {listen_port};' in block:
            return block
    assert False, f'no server block listening on {listen_port}'


def _marker_headers(server_block):
    """Every proxy_set_header X-Trusted-Surface value in a server block."""
    return re.findall(
        r'proxy_set_header X-Trusted-Surface\s+(\S+);', server_block
    )


@pytest.fixture(scope='module')
def conf_text():
    return NGINX_CONF.read_text()


class TestHostAllowlists:
    def test_public_map_allows_only_funnel_hostname(self, conf_text):
        entries = _map_entries(_extract_block(
            conf_text, r'map \$host \$host_allowed_public'
        ))
        assert entries == {FUNNEL_HOST}

    def test_public_map_has_no_loopback_entries(self, conf_text):
        # Loopback entries on :80 let any client spoof a trusted Host and
        # flip the middleware's is_public_host() — the removed hole.
        entries = _map_entries(_extract_block(
            conf_text, r'map \$host \$host_allowed_public'
        ))
        assert not entries & LOOPBACK_HOSTS

    def test_tailnet_map_keeps_loopback_entries(self, conf_text):
        entries = _map_entries(_extract_block(
            conf_text, r'map \$host \$host_allowed_tailnet'
        ))
        assert {'localhost', '127.0.0.1', FUNNEL_HOST} <= entries

    def test_tailnet_map_covers_trusted_local_hosts(self, conf_text):
        # "Keep the tailnet map in sync with TRUSTED_LOCAL_HOSTS" — every
        # host Django trusts must be servable on the tailnet listener.
        from backend.middleware import _LOCAL_HOSTS
        entries = _map_entries(_extract_block(
            conf_text, r'map \$host \$host_allowed_tailnet'
        ))
        assert _LOCAL_HOSTS <= entries


class TestServerGuards:
    def test_public_server_444s_unknown_hosts(self, conf_text):
        public = _server_block(conf_text, 80)
        assert 'if ($host_allowed_public = 0)' in public
        assert 'return 444' in public

    def test_tailnet_server_444s_unknown_hosts(self, conf_text):
        tailnet = _server_block(conf_text, 8081)
        assert 'if ($host_allowed_tailnet = 0)' in tailnet
        assert 'return 444' in tailnet


class TestTrustMarkerWiring:
    def test_public_locations_strip_trust_marker(self, conf_text):
        public = _server_block(conf_text, 80)
        values = _marker_headers(public)
        assert values, 'public server sets no trust marker'
        assert all(v == '""' for v in values), values

    def test_tailnet_locations_set_marker(self, conf_text):
        tailnet = _server_block(conf_text, 8081)
        values = _marker_headers(tailnet)
        assert values, 'tailnet server sets no trust marker'
        assert all(v == '"tailnet"' for v in values), values
