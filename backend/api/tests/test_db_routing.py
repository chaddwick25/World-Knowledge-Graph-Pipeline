from django.contrib.auth.models import User
from django.db import ProgrammingError
from django.test import TestCase


class ApiRoutingTest(TestCase):
    databases = {'default', 'vectors'}

    def test_default_db_routing(self):
        """Verify that default-routed models save to the 'default' database."""
        test_object = User.objects.create(username="test_default_db")

        self.assertTrue(User.objects.using('default').filter(pk=test_object.pk).exists())

        # Querying on 'vectors' raises an error because auth only migrates
        # on 'default' — the auth_user table does not exist there.
        with self.assertRaises(ProgrammingError):
            User.objects.using('vectors').filter(pk=test_object.pk).exists()
