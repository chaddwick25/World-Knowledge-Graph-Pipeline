from django.test import TestCase
from django.db import ProgrammingError
from .models import ApiTestModel

class ApiRoutingTest(TestCase):
    databases = {'default', 'vectors'}

    def test_api_model_saves_to_default_db(self):
        """Verify that ApiTestModel is saved to the 'default' database."""
        # Create an object and save it
        test_object = ApiTestModel.objects.create(name="test_default_db")

        # Verify the object exists in the 'default' database
        self.assertTrue(ApiTestModel.objects.using('default').filter(pk=test_object.pk).exists())

        # Verify that querying for the object in the 'vectors' database raises an error
        # because the table does not exist there.
        with self.assertRaises(ProgrammingError):
            ApiTestModel.objects.using('vectors').filter(pk=test_object.pk).exists()
