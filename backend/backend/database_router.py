class ShardRouter:
    """Route pipeline models to the correct shard DB.

    Currently a no-op (returns None → falls through to AppRouter →
    'default'). When sharding lands, this router reads
    StorageTarget.db_alias from the envelope context and routes
    PipelineLogEntry / PipelineRun writes to the shard DB.
    """

    shard_models = {'PipelineLogEntry', 'PipelineRun'}

    def db_for_read(self, model, **hints):
        if model.__name__ in self.shard_models:
            db_alias = hints.get('db_alias')
            if db_alias:
                return db_alias
        return None

    def db_for_write(self, model, **hints):
        if model.__name__ in self.shard_models:
            db_alias = hints.get('db_alias')
            if db_alias:
                return db_alias
        return None

    def allow_relation(self, obj1, obj2, **hints):
        return None

    def allow_migrate(self, db, app_label, model_name=None, **hints):
        if model_name in ('pipelinelogentry', 'pipelinerun'):
            return db == 'default'  # Migrations always on default
        return None


class AppRouter:
    """Route database operations for extraction, analysis, orchestration apps."""
    
    def db_for_read(self, model, **hints):
        # if model._meta.app_label in ['extraction', 'analysis', 'orchestration', 'toronto_data']:
        if model._meta.app_label in ['extraction', 'analysis', 'orchestration']:
            return 'default'
        return None
    
    def db_for_write(self, model, **hints):
        # if model._meta.app_label in ['extraction', 'analysis', 'orchestration', 'toronto_data']:
        if model._meta.app_label in ['extraction', 'analysis', 'orchestration']:
            return 'default'
        return None
    
    def allow_relation(self, obj1, obj2, **hints):
        # app_labels = {'extraction', 'analysis', 'orchestration', 'toronto_data'}
        app_labels = {'extraction', 'analysis', 'orchestration'}
        if obj1._meta.app_label in app_labels and obj2._meta.app_label in app_labels:
            return True
        return None
    
    def allow_migrate(self, db, app_label, model_name=None, **hints):
        # if app_label in ['extraction', 'analysis', 'orchestration', 'toronto_data']:
        if app_label in ['extraction', 'analysis', 'orchestration']:
            return db == 'default'
        return None


class VectorDBRouter:
    """
    A router to control all database operations on models in the
    vectors application (and graph_analysis if it exists).
    """
    route_app_labels = {'vectors', 'graph_analysis', 'igea', 'worldkg_nca'}

    def db_for_read(self, model, **hints):
        if model._meta.app_label in self.route_app_labels:
            return 'vectors'
        return None

    def db_for_write(self, model, **hints):
        if model._meta.app_label in self.route_app_labels:
            return 'vectors'
        return None

    def allow_relation(self, obj1, obj2, **hints):
        if (
            obj1._meta.app_label in self.route_app_labels or
            obj2._meta.app_label in self.route_app_labels
        ):
           return obj1._meta.app_label == obj2._meta.app_label
        return None

    def allow_migrate(self, db, app_label, model_name=None, **hints):
        if app_label in self.route_app_labels:
            return db == 'vectors'
        return db == 'default'
