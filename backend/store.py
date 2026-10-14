"""Pick the storage backend: Postgres/Supabase when DATABASE_URL is set, otherwise a SQLite file."""
from . import config


def open_database():
    if config.DATABASE_URL:
        from .pg import PostgresDatabase  # imported lazily: SQLite-only setups need no driver
        return PostgresDatabase(config.DATABASE_URL)
    from .db import Database
    return Database(config.DB_PATH)
