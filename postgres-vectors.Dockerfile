# Custom Postgres image for the vectors database
# Includes PostGIS and pgvector server extensions for PostgreSQL 15.

FROM postgres:15

# Install PostGIS + pgvector server extensions
RUN apt-get update -y \
    && apt-get install -y --no-install-recommends \
       postgis postgresql-15-postgis-3 postgresql-15-postgis-3-scripts \
       postgresql-15-pgvector \
    && rm -rf /var/lib/apt/lists/*

# Note: extension creation (CREATE EXTENSION postgis/vector) is handled
# by Django and/or project setup scripts against the target database.
