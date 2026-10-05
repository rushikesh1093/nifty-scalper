"""
Run this once to:
  1. Create the nifty_scalper database
  2. Enable TimescaleDB extension
  3. Run Alembic migrations
"""
import subprocess
import sys
import os
import psycopg2
from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT

DB_HOST = "localhost"
DB_PORT = 5432
DB_USER = "postgres"
DB_PASS = "1093"
DB_NAME = "nifty_scalper"


def create_database():
    print(f"[1/3] Connecting to postgres to create database '{DB_NAME}'...")
    conn = psycopg2.connect(
        host=DB_HOST, port=DB_PORT,
        user=DB_USER, password=DB_PASS,
        dbname="postgres"
    )
    conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
    cur = conn.cursor()
    cur.execute(f"SELECT 1 FROM pg_database WHERE datname = '{DB_NAME}'")
    if cur.fetchone():
        print(f"   Database '{DB_NAME}' already exists, skipping create.")
    else:
        cur.execute(f"CREATE DATABASE {DB_NAME}")
        print(f"   Created database '{DB_NAME}'.")
    cur.close()
    conn.close()


def enable_timescaledb():
    print("[2/3] Checking TimescaleDB extension...")
    conn = psycopg2.connect(
        host=DB_HOST, port=DB_PORT,
        user=DB_USER, password=DB_PASS,
        dbname=DB_NAME
    )
    conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
    cur = conn.cursor()
    try:
        cur.execute("CREATE EXTENSION IF NOT EXISTS timescaledb CASCADE;")
        print("   TimescaleDB extension enabled.")
    except Exception as e:
        print(f"   TimescaleDB not available ({e.__class__.__name__}): skipping.")
        print("   Running in plain PostgreSQL mode (hypertables disabled).")
    cur.close()
    conn.close()


def run_migrations():
    print("[3/3] Running Alembic migrations...")
    # Use sys.executable so we always use the same Python/venv running this script
    alembic_cmd = [sys.executable, "-m", "alembic", "upgrade", "head"]
    result = subprocess.run(
        alembic_cmd,
        capture_output=True, text=True,
        cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # backend/
    )
    print(result.stdout)
    if result.returncode != 0:
        print("ERROR:", result.stderr)
        sys.exit(1)
    print("   Migrations complete.")


if __name__ == "__main__":
    create_database()
    enable_timescaledb()
    run_migrations()
    print("\n✓ Database ready.")
