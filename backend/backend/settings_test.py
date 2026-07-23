# Test settings: import base settings and override DATABASES to use SQLite in-memory DBs
from .settings import *
import tempfile

# Use a temporary directory for downloads during tests
DOWNLOADS_DIR = tempfile.gettempdir()  # noqa

# Use PostgreSQL test databases
DATABASES = {
    'default': dj_database_url.config(
        default=f"postgres://{os.getenv('POSTGRES_USER')}:{os.getenv('POSTGRES_PASSWORD')}@localhost:5434/{os.getenv('POSTGRES_DB')}_test",
        conn_max_age=600
    ),
    'vectors': dj_database_url.config(
        default=f"postgres://{os.getenv('PGVECTOR_USER')}:{os.getenv('PGVECTOR_PASSWORD')}@localhost:5435/{os.getenv('PGVECTOR_DB')}_test",
        conn_max_age=600
    )
}

# Speed up password hashing during tests
PASSWORD_HASHERS = [
    'django.contrib.auth.hashers.MD5PasswordHasher',
]

# Optional: silence logging noise in tests
LOGGING = {
    'version': 1,
    'disable_existing_loggers': True,
}
