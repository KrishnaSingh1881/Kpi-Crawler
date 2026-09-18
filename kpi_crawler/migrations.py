"""Small, explicit SQL migration runner."""

from dataclasses import dataclass
import hashlib
from pathlib import Path
import re

from .db import connection
from .errors import DatabaseError

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
MIGRATION_NAME = re.compile(r"^(?P<version>\d{4})_(?P<name>[a-z0-9_]+)\.sql$")


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sql: str
    checksum: str


def discover(directory: Path = MIGRATIONS_DIR) -> list[Migration]:
    migrations = []
    for path in sorted(directory.glob("*.sql")):
        match = MIGRATION_NAME.fullmatch(path.name)
        if match is None:
            raise ValueError(f"invalid migration filename: {path.name}")
        sql = path.read_text(encoding="utf-8")
        migrations.append(
            Migration(
                version=int(match["version"]),
                name=match["name"],
                sql=sql,
                checksum=hashlib.sha256(sql.encode("utf-8")).hexdigest(),
            )
        )
    if len({migration.version for migration in migrations}) != len(migrations):
        raise ValueError("migration versions must be unique")
    return migrations


def migrate(database_url: str, directory: Path = MIGRATIONS_DIR) -> int:
    """Apply pending migrations and return the number applied."""
    migrations = discover(directory)
    try:
        with connection(database_url) as conn:
            conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", ("kpi_crawler:migrations",))
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    checksum TEXT NOT NULL,
                    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )
            applied = {
                row[0]: row[1]
                for row in conn.execute("SELECT version, checksum FROM schema_migrations")
            }
            count = 0
            for migration in migrations:
                if migration.version in applied:
                    if applied[migration.version] != migration.checksum:
                        raise DatabaseError(
                            f"migration {migration.version:04d} checksum does not match"
                        )
                    continue
                conn.execute(migration.sql)
                conn.execute(
                    "INSERT INTO schema_migrations (version, name, checksum) VALUES (%s, %s, %s)",
                    (migration.version, migration.name, migration.checksum),
                )
                count += 1
            return count
    except DatabaseError:
        raise
