# Custom Postgres image for the default app database
# Includes PostGIS and pgvector server extensions for PostgreSQL 17.

FROM postgres:17

# Install PostGIS + pgvector server extensions
RUN apt-get update -y \
    && apt-get install -y --no-install-recommends \
       postgis postgresql-17-postgis-3 postgresql-17-postgis-3-scripts \
       postgresql-17-pgvector \
    && rm -rf /var/lib/apt/lists/*

# Note: extension creation (CREATE EXTENSION postgis/vector) is handled
# by Django and/or project setup scripts against the target database.
