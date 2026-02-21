"""Maintenance helpers for upgrades/backups/migration status.

These are intentionally simple wrappers so admins do not need to run raw
Alembic commands.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Any

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from dotenv import load_dotenv
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import URL, make_url

REPO_ROOT = Path(__file__).resolve().parents[1]
DOTENV_PATH = REPO_ROOT / ".env"
ALEMBIC_INI_PATH = REPO_ROOT / "alembic.ini"
DEFAULT_BACKUP_DIR = REPO_ROOT / "backups"
CORE_TABLES = {
    "app_state",
    "characters",
    "corp_settings",
    "deliveries",
    "esi_state",
    "notifications",
}

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.config import get_settings


def _utc_stamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def _load_env() -> None:
    load_dotenv(DOTENV_PATH, override=True)


def _settings():
    _load_env()
    get_settings.cache_clear()
    return get_settings()


def _db_url() -> str:
    return _settings().database_url


def _create_engine():
    url = _db_url()
    connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
    return create_engine(url, future=True, connect_args=connect_args)


def _alembic_config() -> Config:
    cfg = Config(str(ALEMBIC_INI_PATH))
    cfg.set_main_option("script_location", str(REPO_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", _db_url())
    return cfg


def run_preflight() -> dict[str, Any]:
    """Validate environment and DB connectivity before upgrades."""
    errors: list[str] = []
    warnings: list[str] = []
    settings = _settings()

    if not ALEMBIC_INI_PATH.exists():
        errors.append(f"Missing Alembic config: {ALEMBIC_INI_PATH}")
    if not (REPO_ROOT / "alembic" / "env.py").exists():
        errors.append("Missing alembic/env.py")
    if not any((REPO_ROOT / "alembic" / "versions").glob("*.py")):
        errors.append("No Alembic revisions found in alembic/versions")

    if settings.env.lower() == "prod":
        if settings.session_secret == "dev-session-secret-change-me":
            warnings.append("SESSION_SECRET is still set to default dev value.")
        if settings.csrf_secret == "dev-csrf-secret-change-me":
            warnings.append("CSRF_SECRET is still set to default dev value.")
        if settings.database_url.startswith("sqlite"):
            warnings.append("Production mode is using SQLite; PostgreSQL is recommended.")

    try:
        engine = _create_engine()
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        errors.append(f"Database connectivity check failed: {exc}")

    return {
        "ok": len(errors) == 0,
        "database_url": settings.database_url,
        "env": settings.env,
        "errors": errors,
        "warnings": warnings,
    }


def _sqlite_backup_path(url: URL) -> Path:
    if not url.database:
        raise ValueError("SQLite URL is missing database path")
    db_path = Path(url.database)
    if not db_path.is_absolute():
        db_path = REPO_ROOT / db_path
    return db_path


def run_backup(output_dir: Path | None = None) -> dict[str, Any]:
    """Create a DB backup appropriate for current backend."""
    output_dir = output_dir or DEFAULT_BACKUP_DIR
    output_dir.mkdir(parents=True, exist_ok=True)

    url = make_url(_db_url())
    stamp = _utc_stamp()

    if url.get_backend_name().startswith("sqlite"):
        source = _sqlite_backup_path(url)
        if not source.exists():
            raise FileNotFoundError(f"SQLite DB file not found: {source}")
        target = output_dir / f"condottiere-sqlite-{stamp}.db"
        shutil.copy2(source, target)
        return {
            "backend": "sqlite",
            "source": str(source),
            "backup_path": str(target),
        }

    if url.get_backend_name().startswith("postgresql"):
        target = output_dir / f"condottiere-postgres-{stamp}.dump"
        cmd = [
            "pg_dump",
            "--format=custom",
            f"--file={target}",
            _db_url(),
        ]
        completed = subprocess.run(cmd, check=False, capture_output=True, text=True)
        if completed.returncode != 0:
            raise RuntimeError(
                f"pg_dump failed ({completed.returncode}): {completed.stderr.strip()}"
            )
        return {
            "backend": "postgresql",
            "backup_path": str(target),
        }

    raise ValueError(f"Unsupported database backend: {url.get_backend_name()}")


def _db_has_existing_schema_without_alembic_version() -> bool:
    engine = _create_engine()
    inspector = inspect(engine)
    table_names = set(inspector.get_table_names())
    return "alembic_version" not in table_names and bool(table_names.intersection(CORE_TABLES))


def run_upgrade(*, auto_stamp_existing: bool = True) -> dict[str, Any]:
    """Run Alembic upgrade; optionally stamp existing unmanaged schemas."""
    cfg = _alembic_config()
    stamped = False
    if auto_stamp_existing and _db_has_existing_schema_without_alembic_version():
        command.stamp(cfg, "head")
        stamped = True
    command.upgrade(cfg, "head")
    return {"upgraded": True, "stamped_existing_schema": stamped}


def run_db_status() -> dict[str, Any]:
    """Return current DB revision and migration head(s)."""
    cfg = _alembic_config()
    script = ScriptDirectory.from_config(cfg)
    heads = script.get_heads()
    head = heads[0] if len(heads) == 1 else ",".join(heads)

    engine = _create_engine()
    with engine.connect() as conn:
        context = MigrationContext.configure(conn)
        current = context.get_current_revision()

    return {
        "current_revision": current,
        "head_revision": head,
        "is_up_to_date": bool(current and current in heads),
    }


def run_update_software() -> dict[str, Any]:
    """One-command maintenance flow: preflight -> backup -> upgrade -> status."""
    preflight = run_preflight()
    if not preflight["ok"]:
        return {
            "ok": False,
            "step": "preflight",
            "preflight": preflight,
        }

    backup = run_backup()
    upgrade = run_upgrade(auto_stamp_existing=True)
    status = run_db_status()
    return {
        "ok": True,
        "preflight": preflight,
        "backup": backup,
        "upgrade": upgrade,
        "status": status,
    }
