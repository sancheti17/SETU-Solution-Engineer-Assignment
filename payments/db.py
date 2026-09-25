"""Connection-per-request SQLite, explicit transactions, and a v1 migration."""
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from flask import current_app, g


def connect(path, timeout_ms=5000):
    connection = sqlite3.connect(str(path), isolation_level=None, timeout=timeout_ms / 1000)
    connection.row_factory = sqlite3.Row
    connection.execute('PRAGMA foreign_keys=ON')
    connection.execute(f'PRAGMA busy_timeout={int(timeout_ms)}')
    connection.execute('PRAGMA synchronous=FULL')
    return connection


def initialize(path):
    if str(path) == ':memory:':
        raise ValueError('Use a file-backed database; per-request connections cannot share :memory:.')
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    db = connect(path)
    try:
        db.execute('PRAGMA journal_mode=WAL')
        version = db.execute('PRAGMA user_version').fetchone()[0]
        if version not in (0, 1):
            raise RuntimeError(f'Unsupported schema version: {version}')
        if version == 0:
            schema = Path(__file__).with_name('schema.sql').read_text()
            db.executescript('BEGIN IMMEDIATE;\n' + schema + '\nCOMMIT;')
    finally:
        db.close()


def get_db():
    if 'db' not in g:
        g.db = connect(current_app.config['DATABASE_PATH'], current_app.config['DB_TIMEOUT_MS'])
    return g.db


def close_db(_error=None):
    db = g.pop('db', None)
    if db is not None:
        db.close()


@contextmanager
def transaction(db, write=False):
    db.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
    try:
        yield db
        db.commit()
    except BaseException:
        db.rollback()
        raise
