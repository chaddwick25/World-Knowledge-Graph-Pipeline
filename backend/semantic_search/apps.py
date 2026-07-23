from django.apps import AppConfig

# Remove after the testing Bert
class SemanticSearchConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'semantic_search'

    def ready(self):
        """Pre-load FastText model at startup to avoid loading on every request."""
        from semantic_search.services.model_registry_service import ModelRegistryService
        # Pre-load FastText model
        ModelRegistryService.get_fasttext_model()
