"""Pytest configuration for the WorldKG backend.

Sets up Django for all tests. Database access is allowed by default
via the django_db marker auto-applied to all tests.

The Docker Compose environment has both 'default' and 'vectors' databases
running. Integration tests that need real DB data should use countries
that have completed pipeline runs (e.g. CV for Cape Verde).
"""

import os
import django
import pytest

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "backend.settings")
django.setup()


@pytest.fixture(autouse=True)
def _allow_db(request):
    """Auto-apply django_db marker to allow DB access for all tests.

    Uses transaction=False: writes COMMIT to the test databases
    (test_django_db / test_vector_db) and are NOT rolled back between
    tests, so DB-writing tests must clean up after themselves. The
    pytest-django test runner always redirects connections to the test
    databases — this suite never touches the live django_db / vector_db.
    """
    if not request.node.get_closest_marker("django_db"):
        request.node.add_marker(pytest.mark.django_db(transaction=False))
