"""Temporary verification databases on the existing local PostgreSQL server."""

import uuid
from collections.abc import Iterator
from contextlib import contextmanager

import psycopg
from psycopg import sql

from project_log.db import local_dsn


@contextmanager
def temporary_database() -> Iterator[str]:
    # Deliberately ignore the product URL override: tests never write to that database.
    name = "project_log_verify_" + uuid.uuid4().hex
    admin = local_dsn("postgres")
    with psycopg.connect(admin, autocommit=True, connect_timeout=5) as conn:
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    try:
        yield local_dsn(name)
    finally:
        # Only the database successfully created by this invocation is dropped.
        with psycopg.connect(admin, autocommit=True, connect_timeout=5) as conn:
            conn.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))
