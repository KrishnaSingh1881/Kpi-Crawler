"""PostgreSQL connection boundary."""

from contextlib import contextmanager
from typing import Iterator

import psycopg

from .errors import DatabaseError


@contextmanager
def connection(database_url: str) -> Iterator[psycopg.Connection]:
    """Yield one transaction-scoped PostgreSQL connection."""
    try:
        with psycopg.connect(database_url) as conn:
            yield conn
    except psycopg.Error as exc:
        raise DatabaseError(f"PostgreSQL operation failed: {exc}") from exc
