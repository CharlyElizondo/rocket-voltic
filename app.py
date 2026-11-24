from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from http.cookies import CookieError, SimpleCookie
from html import escape
from contextlib import closing
from urllib.parse import parse_qs, quote
import argparse
import getpass
import hashlib
import json
import os
import secrets
import sqlite3
import time 
from datetime import date, datetime


# ============================================================
# CONFIGURACIÓN
# ============================================================

APP_ENV = os.environ.get("APP_ENV", "").strip().lower()
if not APP_ENV:
    APP_ENV = "production" if os.environ.get("RENDER") else "development"
HOST = os.environ.get("HOST", "0.0.0.0").strip()
try:
    PORT = int(os.environ.get("PORT", "8000"))
except ValueError as error:
    raise ValueError("PORT debe ser un número entero válido.") from error
if not 1 <= PORT <= 65535:
    raise ValueError("PORT debe ser un número entre 1 y 65535.")
COOKIE_SECURE = APP_ENV == "production" or bool(os.environ.get("RENDER"))
SECURE_COOKIE_ATTRIBUTE = "; Secure" if COOKIE_SECURE else ""

# WhatsApp de Rocket & Voltic
WHATSAPP_NUMBER = "50686341818"

USER_DATABASE_PATH = os.environ.get(
    "USER_DATABASE_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "rocket_voltic_users.sqlite3"),
)
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
PASSWORD_HASH_ITERATIONS = 600_000
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "Charlie").strip()
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD")
if ADMIN_PASSWORD is None and APP_ENV != "production":
    ADMIN_PASSWORD = "Charlieadmin2025"
USER_SESSION_MAX_AGE = 8 * 60 * 60
MAX_RECORD_TITLE_LENGTH = 120
MAX_RECORD_DESCRIPTION_LENGTH = 4000
FREE_MONTHLY_REQUEST_LIMIT = 30
COMPANY_VARIANTS = {
    "normal": {"plan": "free", "label": "Normal"},
    "premium": {"plan": "pro", "label": "Premium"},
}
DEFAULT_COMPANY_VARIANT = "normal"
SERVICE_REQUEST_STATUSES = {
    "pending": "En espera de aceptación",
    "accepted": "Aceptada",
    "denied": "Denegada",
    "in_progress": "En trabajo",
    "completed": "Terminada",
}


def company_plan_for_variant(company_variant):
    if company_variant not in COMPANY_VARIANTS:
        raise ValueError("La versión de Rocket & Voltic no es válida.")
    return COMPANY_VARIANTS[company_variant]["plan"]


def version_switch_html(company_variant):
    selected_label = COMPANY_VARIANTS[company_variant]["label"]
    return (
        '<div class="version-switch" aria-label="Cambiar versión de demostración">'
        f"<span>Demo activa: {escape(selected_label)}</span>"
        '<a href="/select-company">Cambiar versión</a>'
        '<a href="/account/login?next=admin">Iniciar como administrador</a>'
        "</div>"
    )


class PostgresCursor:
    def __init__(self, cursor):
        self.cursor = cursor

    @staticmethod
    def normalize_row(row):
        if row is None:
            return None
        return tuple(
            value.isoformat(sep=" ") if isinstance(value, datetime)
            else value.isoformat() if isinstance(value, date)
            else value
            for value in row
        )

    def fetchone(self):
        return self.normalize_row(self.cursor.fetchone())

    def fetchall(self):
        return [self.normalize_row(row) for row in self.cursor.fetchall()]

    @property
    def rowcount(self):
        return self.cursor.rowcount


class PostgresConnection:
    def __init__(self, connection):
        self.connection = connection

    def execute(self, query, parameters=()):
        query = query.replace("?", "%s")
        query = query.replace(
            "username = %s COLLATE NOCASE",
            "LOWER(username) = LOWER(%s)",
        )
        return PostgresCursor(self.connection.execute(query, parameters))

    def __enter__(self):
        self.connection.__enter__()
        return self

    def __exit__(self, error_type, error, traceback):
        return self.connection.__exit__(error_type, error, traceback)

    def close(self):
        self.connection.close()

    def commit(self):
        self.connection.commit()


def connect_user_database():
    if DATABASE_URL:
        try:
            import psycopg
        except ImportError as error:
            raise RuntimeError(
                "Instala psycopg para conectarte a PostgreSQL."
            ) from error
        return PostgresConnection(psycopg.connect(DATABASE_URL, connect_timeout=10))

    database_directory = os.path.dirname(os.path.abspath(USER_DATABASE_PATH))
    if database_directory:
        os.makedirs(database_directory, exist_ok=True)
    connection = sqlite3.connect(USER_DATABASE_PATH, timeout=10)
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def initialize_user_database():
    if DATABASE_URL:
        initialize_postgres_user_database()
        return

    with closing(connect_user_database()) as connection:
        with connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT NOT NULL COLLATE NOCASE UNIQUE,
                    password_salt BLOB NOT NULL,
                    password_hash BLOB NOT NULL,
                    role TEXT NOT NULL DEFAULT 'customer'
                        CHECK (role IN ('customer', 'admin')),
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            user_columns = {
                row[1]
                for row in connection.execute("PRAGMA table_info(users)").fetchall()
            }
            if "role" not in user_columns:
                connection.execute(
                    """
                    ALTER TABLE users
                    ADD COLUMN role TEXT NOT NULL DEFAULT 'customer'
                        CHECK (role IN ('customer', 'admin'))
                    """
                )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS user_sessions (
                    token_hash TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    csrf_hash TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    expires_at INTEGER NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS user_records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS service_requests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    customer_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                    customer_name TEXT NOT NULL,
                    customer_contact TEXT NOT NULL,
                    description TEXT NOT NULL,
                    selected_products TEXT NOT NULL DEFAULT '[]',
                    quoted_total INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'pending'
                        CHECK (status IN (
                            'pending', 'accepted', 'denied', 'in_progress', 'completed'
                        )),
                    solution TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            service_request_columns = {
                row[1]
                for row in connection.execute(
                    "PRAGMA table_info(service_requests)"
                ).fetchall()
            }
            if "company_variant" not in service_request_columns:
                connection.execute(
                    """
                    ALTER TABLE service_requests
                    ADD COLUMN company_variant TEXT NOT NULL DEFAULT 'normal'
                        CHECK (company_variant IN ('normal', 'premium'))
                    """
                )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_user_records_owner "
                "ON user_records(user_id, updated_at)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_service_requests_customer "
                "ON service_requests(customer_user_id, created_at)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_service_requests_created "
                "ON service_requests(created_at)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_service_requests_variant_created "
                "ON service_requests(company_variant, created_at)"
            )


def initialize_postgres_user_database():
    with closing(connect_user_database()) as connection:
        with connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id BIGSERIAL PRIMARY KEY,
                    username TEXT NOT NULL UNIQUE,
                    password_salt BYTEA NOT NULL,
                    password_hash BYTEA NOT NULL,
                    role TEXT NOT NULL DEFAULT 'customer'
                        CHECK (role IN ('customer', 'admin')),
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            connection.execute(
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS role TEXT NOT NULL "
                "DEFAULT 'customer' CHECK (role IN ('customer', 'admin'))"
            )
            connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_username_lower "
                "ON users (LOWER(username))"
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS user_sessions (
                    token_hash TEXT PRIMARY KEY,
                    user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    csrf_hash TEXT NOT NULL,
                    created_at BIGINT NOT NULL,
                    expires_at BIGINT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS user_records (
                    id BIGSERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS service_requests (
                    id BIGSERIAL PRIMARY KEY,
                    customer_user_id BIGINT REFERENCES users(id) ON DELETE SET NULL,
                    customer_name TEXT NOT NULL,
                    customer_contact TEXT NOT NULL,
                    description TEXT NOT NULL,
                    selected_products TEXT NOT NULL DEFAULT '[]',
                    quoted_total BIGINT NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'pending'
                        CHECK (status IN (
                            'pending', 'accepted', 'denied', 'in_progress', 'completed'
                        )),
                    solution TEXT NOT NULL DEFAULT '',
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    company_variant TEXT NOT NULL DEFAULT 'normal'
                        CHECK (company_variant IN ('normal', 'premium'))
                )
                """
            )
            connection.execute(
                "ALTER TABLE service_requests ADD COLUMN IF NOT EXISTS "
                "company_variant TEXT NOT NULL DEFAULT 'normal' "
                "CHECK (company_variant IN ('normal', 'premium'))"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_user_records_owner "
                "ON user_records(user_id, updated_at)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_service_requests_customer "
                "ON service_requests(customer_user_id, created_at)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_service_requests_created "
                "ON service_requests(created_at)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_service_requests_variant_created "
                "ON service_requests(company_variant, created_at)"
            )


def ensure_bootstrap_admin():
    if not ADMIN_PASSWORD:
        raise RuntimeError(
            "En producción configura ADMIN_PASSWORD con la contraseña del administrador."
        )
    if not ADMIN_USERNAME or len(ADMIN_USERNAME) > 64:
        raise RuntimeError("ADMIN_USERNAME debe tener entre 1 y 64 caracteres.")
    if len(ADMIN_PASSWORD) < 12 or len(ADMIN_PASSWORD) > 1024:
        raise RuntimeError("ADMIN_PASSWORD debe tener entre 12 y 1024 caracteres.")

    salt = secrets.token_bytes(16)
    password_hash = hashlib.pbkdf2_hmac(
        "sha256",
        ADMIN_PASSWORD.encode("utf-8"),
        salt,
        PASSWORD_HASH_ITERATIONS,
    )
    with closing(connect_user_database()) as connection:
        with connection:
            user = connection.execute(
                "SELECT id FROM users WHERE username = ? COLLATE NOCASE",
                (ADMIN_USERNAME,),
            ).fetchone()
            if user is None:
                connection.execute(
                    """
                    INSERT INTO users (username, password_salt, password_hash, role)
                    VALUES (?, ?, ?, 'admin')
                    """,
                    (ADMIN_USERNAME, salt, password_hash),
                )
            else:
                connection.execute(
                    """
                    UPDATE users
                    SET password_salt = ?, password_hash = ?, role = 'admin'
                    WHERE id = ?
                    """,
                    (salt, password_hash, user[0]),
                )
                connection.execute(
                    "DELETE FROM user_sessions WHERE user_id = ?",
                    (user[0],),
                )


def register_user(username, password, role="customer"):
    username = username.strip()
    if not username or len(username) > 64 or any(ord(char) < 32 for char in username):
        raise ValueError("El usuario debe tener entre 1 y 64 caracteres válidos.")
    if role not in ("customer", "admin"):
        raise ValueError("El rol de usuario no es válido.")
    if len(password) < 12:
        raise ValueError("La contraseña debe tener al menos 12 caracteres.")
    if len(password) > 1024:
        raise ValueError("La contraseña no puede superar los 1024 caracteres.")

    salt = secrets.token_bytes(16)
    password_hash = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        PASSWORD_HASH_ITERATIONS,
    )
    try:
        with closing(connect_user_database()) as connection:
            with connection:
                connection.execute(
                    """
                    INSERT INTO users (username, password_salt, password_hash, role)
                    VALUES (?, ?, ?, ?)
                    """,
                    (username, salt, password_hash, role),
                )
    except Exception as error:
        if (
            isinstance(error, sqlite3.IntegrityError)
            or error.__class__.__name__ == "UniqueViolation"
        ):
            raise ValueError("Ya existe una cuenta con ese usuario.") from error
        raise


def authenticate_user(username, password):
    with closing(connect_user_database()) as connection:
        with connection:
            user = connection.execute(
                """
                SELECT id, role, password_salt, password_hash
                FROM users
                WHERE username = ? COLLATE NOCASE
                """,
                (username.strip(),),
            ).fetchone()

    if user is None:
        return None

    user_id, role, salt, expected_hash = user
    actual_hash = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        PASSWORD_HASH_ITERATIONS,
    )
    return (user_id, role) if secrets.compare_digest(actual_hash, expected_hash) else None


def hash_session_secret(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def create_user_session(user_id, token, csrf_token):
    now = int(time.time())
    with closing(connect_user_database()) as connection:
        with connection:
            connection.execute(
                "DELETE FROM user_sessions WHERE expires_at <= ?",
                (now,),
            )
            connection.execute(
                """
                INSERT INTO user_sessions
                    (token_hash, user_id, csrf_hash, created_at, expires_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    hash_session_secret(token),
                    user_id,
                    hash_session_secret(csrf_token),
                    now,
                    now + USER_SESSION_MAX_AGE,
                ),
            )


def list_user_records(user_id):
    with closing(connect_user_database()) as connection:
        rows = connection.execute(
            """
            SELECT id, title, description, created_at, updated_at
            FROM user_records
            WHERE user_id = ?
            ORDER BY updated_at DESC, id DESC
            """,
            (user_id,),
        ).fetchall()
    return [
        {
            "id": row[0],
            "title": row[1],
            "description": row[2],
            "created_at": row[3],
            "updated_at": row[4],
        }
        for row in rows
    ]


def get_user_record(user_id, record_id):
    with closing(connect_user_database()) as connection:
        row = connection.execute(
            """
            SELECT id, title, description, created_at, updated_at
            FROM user_records
            WHERE id = ? AND user_id = ?
            """,
            (record_id, user_id),
        ).fetchone()
    if row is None:
        return None
    return {
        "id": row[0],
        "title": row[1],
        "description": row[2],
        "created_at": row[3],
        "updated_at": row[4],
    }


def create_user_record(user_id, title, description):
    with closing(connect_user_database()) as connection:
        with connection:
            cursor = connection.execute(
                """
                INSERT INTO user_records (user_id, title, description)
                VALUES (?, ?, ?)
                """ + (" RETURNING id" if DATABASE_URL else ""),
                (user_id, title, description),
            )
            if DATABASE_URL:
                record_id = cursor.fetchone()[0]
            else:
                record_id = cursor.lastrowid
    return get_user_record(user_id, record_id)


def update_user_record(user_id, record_id, title, description):
    with closing(connect_user_database()) as connection:
        with connection:
            cursor = connection.execute(
                """
                UPDATE user_records
                SET title = ?, description = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND user_id = ?
                """,
                (title, description, record_id, user_id),
            )
    return get_user_record(user_id, record_id) if cursor.rowcount else None


def delete_user_record(user_id, record_id):
    with closing(connect_user_database()) as connection:
        with connection:
            cursor = connection.execute(
                "DELETE FROM user_records WHERE id = ? AND user_id = ?",
                (record_id, user_id),
            )
    return cursor.rowcount > 0


class ServiceRequestLimitReached(Exception):
    pass


def current_month_request_count(connection=None, company_variant=DEFAULT_COMPANY_VARIANT):
    owns_connection = connection is None
    if owns_connection:
        connection = connect_user_database()
    try:
        monthly_filter = (
            "created_at >= DATE_TRUNC('month', CURRENT_TIMESTAMP)"
            if DATABASE_URL else
            "strftime('%Y-%m', created_at) = strftime('%Y-%m', 'now')"
        )
        return connection.execute(
            f"""
            SELECT COUNT(*)
            FROM service_requests
            WHERE company_variant = ?
              AND {monthly_filter}
            """,
            (company_variant,),
        ).fetchone()[0]
    finally:
        if owns_connection:
            connection.close()


def create_service_request(
    customer_user_id,
    customer_name,
    customer_contact,
    description,
    selected_products,
    quoted_total,
    company_variant=DEFAULT_COMPANY_VARIANT,
):
    if company_variant not in COMPANY_VARIANTS:
        raise ValueError("La versión de Rocket & Voltic no es válida.")
    company_plan = COMPANY_VARIANTS[company_variant]["plan"]
    with closing(connect_user_database()) as connection:
        if DATABASE_URL:
            connection.execute(
                "SELECT pg_advisory_xact_lock(hashtext(?))",
                (company_variant,),
            )
        else:
            connection.execute("BEGIN IMMEDIATE")
        if (
            company_plan == "free"
            and current_month_request_count(connection, company_variant)
            >= FREE_MONTHLY_REQUEST_LIMIT
        ):
            raise ServiceRequestLimitReached
        cursor = connection.execute(
            """
            INSERT INTO service_requests (
                customer_user_id, customer_name, customer_contact, description,
                selected_products, quoted_total, company_variant
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """ + (" RETURNING id" if DATABASE_URL else ""),
            (
                customer_user_id,
                customer_name,
                customer_contact,
                description,
                json.dumps(selected_products, ensure_ascii=False),
                quoted_total,
                company_variant,
            ),
        )
        if DATABASE_URL:
            request_id = cursor.fetchone()[0]
        else:
            request_id = cursor.lastrowid
        connection.commit()
    return request_id


def _service_request_from_row(row):
    return {
        "id": row[0],
        "customer_name": row[1],
        "customer_contact": row[2],
        "description": row[3],
        "selected_products": json.loads(row[4]),
        "quoted_total": row[5],
        "status": row[6],
        "status_label": SERVICE_REQUEST_STATUSES[row[6]],
        "solution": row[7],
        "created_at": row[8],
        "updated_at": row[9],
    }


def list_customer_service_requests(user_id, company_variant=DEFAULT_COMPANY_VARIANT):
    with closing(connect_user_database()) as connection:
        rows = connection.execute(
            """
            SELECT id, customer_name, customer_contact, description,
                   selected_products, quoted_total, status, solution,
                   created_at, updated_at
            FROM service_requests
            WHERE customer_user_id = ? AND company_variant = ?
            ORDER BY created_at DESC, id DESC
            """,
            (user_id, company_variant),
        ).fetchall()
    return [_service_request_from_row(row) for row in rows]


def list_admin_service_requests(company_variant=DEFAULT_COMPANY_VARIANT):
    with closing(connect_user_database()) as connection:
        rows = connection.execute(
            """
            SELECT id, customer_name, customer_contact, description,
                   selected_products, quoted_total, status, solution,
                   created_at, updated_at
            FROM service_requests
            WHERE company_variant = ?
            ORDER BY
                CASE status
                    WHEN 'pending' THEN 0
                    WHEN 'accepted' THEN 1
                    WHEN 'in_progress' THEN 2
                    ELSE 3
                END,
                created_at DESC,
                id DESC
            """,
            (company_variant,),
        ).fetchall()
    return [_service_request_from_row(row) for row in rows]


def update_service_request(
    request_id,
    status,
    solution,
    company_variant=DEFAULT_COMPANY_VARIANT,
):
    with closing(connect_user_database()) as connection:
        with connection:
            current = connection.execute(
                """
                SELECT status FROM service_requests
                WHERE id = ? AND company_variant = ?
                """,
                (request_id, company_variant),
            ).fetchone()
            if current is None:
                return None

            allowed_transitions = {
                "pending": {"accepted", "denied"},
                "accepted": {"denied", "in_progress"},
                "in_progress": {"completed"},
                "denied": set(),
                "completed": set(),
            }
            if status != current[0] and status not in allowed_transitions[current[0]]:
                raise ValueError("Ese cambio de estado no está permitido.")

            connection.execute(
                """
                UPDATE service_requests
                SET status = ?, solution = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND company_variant = ?
                """,
                (status, solution, request_id, company_variant),
            )
    return True


# ============================================================
# COTIZACIONES
# ============================================================
# Modifica esta lista para cambiar los productos y precios que
# aparecerán en la sección de cotizaciones.
COTIZACIONES = [
    {"producto": "Revisión general de equipo", "precio": "₡25.000"},
    {"producto": "Formateo e instalación de Windows", "precio": "₡30.000"},
    {"producto": "Instalación de antivirus", "precio": "₡18.000"},
    {"producto": "Reparación de software básico", "precio": "₡35.000"},
    {"producto": "Mantenimiento preventivo", "precio": "₡40.000"},
    {"producto": "Servicio de diagnóstico técnico", "precio": "₡20.000"},
]


# ============================================================
# PÁGINA PRINCIPAL
# ============================================================ 

def get_landing_page(
    account_username=None,
    account_role="customer",
    company_variant=DEFAULT_COMPANY_VARIANT,
):
    if account_username:
        account_links_html = '<a href="/dashboard">Mi panel</a>'
        if account_role == "admin":
            account_links_html += '<a href="/admin/requests">Solicitudes</a>'
        account_links_html += '<a href="/account/logout">Cerrar sesión</a>'
    else:
        account_links_html = (
            '<a href="/account/login">Iniciar sesión</a>'
        )
    return """<!DOCTYPE html>
<html lang="es">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <meta name="theme-color" content="#02070d">
    <title>Bienvenido | Rocket &amp; Voltic</title>
    <style>
        * { box-sizing: border-box; }
        body {
            min-height: 100vh;
            margin: 0;
            padding: 24px;
            display: grid;
            place-items: center;
            color: #fff;
            font-family: Arial, Helvetica, sans-serif;
            background: radial-gradient(circle at top, #06243d 0%, #02070d 55%, #000 100%);
        }
        .top-links {
            position: fixed;
            top: 20px;
            left: 20px;
            right: 20px;
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 12px;
        }
        .top-links a {
            padding: 11px 15px;
            border: 1px solid #24445c;
            border-radius: 8px;
            background: #07111b;
            color: #8bcfff;
            font-size: 14px;
            font-weight: bold;
            text-decoration: none;
        }
        .top-links a:last-child {
            border-color: #25D366;
            color: #25D366;
        }
        .top-links .version-switch {
            display: flex;
            align-items: center;
            gap: 8px;
            color: #c4d2df;
            font-size: 13px;
        }
        main {
            width: min(100%, 700px);
            padding: clamp(28px, 7vw, 64px) 24px;
            text-align: center;
        }
        .logo {
            width: 112px;
            height: 112px;
            margin: 0 auto 24px;
            display: grid;
            place-items: center;
            border: 3px solid #0092fb;
            border-radius: 50%;
            font-size: 54px;
            box-shadow: 0 0 30px rgba(0, 146, 251, .35);
        }
        h1 {
            margin: 0;
            font-size: clamp(32px, 7vw, 52px);
            letter-spacing: .5px;
        }
        .subtitle {
            margin: 14px 0 0;
            color: #8bcfff;
            font-size: clamp(14px, 3vw, 18px);
            letter-spacing: 1.5px;
        }
        .welcome {
            max-width: 520px;
            margin: 26px auto 30px;
            color: #c4d2df;
            font-size: 18px;
            line-height: 1.6;
        }
        .start-button {
            min-width: 190px;
            display: inline-block;
            padding: 15px 32px;
            border: 0;
            border-radius: 9px;
            background: #0092fb;
            color: white;
            font-size: 18px;
            font-weight: bold;
            text-decoration: none;
            box-shadow: 0 8px 26px rgba(0, 146, 251, .28);
            transition: background .2s, transform .2s;
        }
        .start-button:hover,
        .start-button:focus-visible {
            background: #007bd3;
            transform: translateY(-2px);
        }
        @media (max-width: 520px) {
            body { padding: 82px 16px 24px; }
            .top-links {
                top: 12px;
                left: 12px;
                right: 12px;
                flex-wrap: wrap;
                justify-content: center;
            }
            .top-links a { padding: 9px 10px; font-size: 12px; }
            .top-links .version-switch { justify-content: center; }
            .logo { width: 92px; height: 92px; font-size: 44px; }
            .welcome { font-size: 16px; }
        }
    </style>
</head>
<body>
    <nav class="top-links" aria-label="Accesos">
        __ACCOUNT_LINKS__
    __VERSION_SWITCH__
    </nav>
    <main>
        <div class="logo" role="img" aria-label="Cohete">🚀</div>
        <h1>Rocket &amp; Voltic</h1>
        <p class="subtitle">REPARACIÓN Y MANTENIMIENTO TECNOLÓGICO</p>
        <p class="welcome">
            ¡Bienvenido! Estamos aquí para ayudarte con tus equipos y resolver
            tus necesidades tecnológicas.
        </p>
        <a class="start-button" href="/contact">Iniciar</a>
    </main>
</body>
</html>""".replace("__ACCOUNT_LINKS__", account_links_html).replace(
        "__VERSION_SWITCH__", version_switch_html(company_variant)
    )


def get_company_selection_page(error_message=""):
    error_html = (
        f'<p class="error" role="alert">{escape(error_message)}</p>'
        if error_message else ""
    )
    return f"""<!DOCTYPE html>
<html lang="es">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <meta name="theme-color" content="#e9ecef">
    <title>Demo | Rocket &amp; Voltic</title>
    <style>
        * {{ box-sizing: border-box; }}
        body {{
            min-height: 100vh;
            margin: 0;
            padding: 24px;
            display: grid;
            place-items: center;
            background: linear-gradient(145deg, #f1f3f5, #dfe3e8);
            color: #28313b;
            font-family: Arial, Helvetica, sans-serif;
        }}
        main {{
            width: min(100%, 480px);
            padding: clamp(26px, 7vw, 46px);
            border: 1px solid #d5d9de;
            border-radius: 16px;
            background: rgba(255, 255, 255, .94);
            box-shadow: 0 18px 50px rgba(34, 45, 56, .12);
        }}
        .eyebrow {{
            margin: 0 0 10px;
            color: #697480;
            font-size: 13px;
            font-weight: bold;
            letter-spacing: 1.8px;
            text-align: center;
            text-transform: uppercase;
        }}
        h1 {{
            margin: 0;
            color: #202832;
            font-size: clamp(26px, 6vw, 34px);
            text-align: center;
        }}
        .intro {{
            margin: 14px 0 28px;
            color: #5f6b77;
            line-height: 1.6;
            text-align: center;
        }}
        label {{
            display: block;
            margin-bottom: 9px;
            color: #37424e;
            font-size: 15px;
            font-weight: bold;
        }}
        select {{
            width: 100%;
            min-height: 50px;
            padding: 0 14px;
            border: 1px solid #c5cbd2;
            border-radius: 8px;
            background: #fff;
            color: #28313b;
            font: inherit;
        }}
        select:focus-visible {{
            outline: 3px solid rgba(88, 110, 132, .25);
            border-color: #687b8f;
        }}
        button {{
            width: 100%;
            min-height: 49px;
            margin-top: 20px;
            border: 0;
            border-radius: 8px;
            background: #4a5662;
            color: #fff;
            font-size: 16px;
            font-weight: bold;
            cursor: pointer;
        }}
        button:hover, button:focus-visible {{ background: #35414c; }}
        .error {{ margin: 0 0 14px; color: #a52c35; }}
        .admin-link {{
            display: block;
            margin-top: 20px;
            color: #2f5f8c;
            font-weight: bold;
            text-align: center;
        }}
    </style>
</head>
<body>
    <main>
        <p class="eyebrow">Demostración para exposición</p>
        <h1>¿Qué versión de Rocket &amp; Voltic quieres ver?</h1>
        <p class="intro">Compara la versión Normal con límite de solicitudes y la versión Premium con seguimiento.</p>
        {error_html}
        <form action="/select-company" method="POST">
            <label for="variant">Versión de Rocket &amp; Voltic</label>
            <select id="variant" name="variant" required>
                <option value="" selected disabled>Selecciona una versión</option>
                <option value="normal">Normal — Gratis, máximo 30 solicitudes al mes</option>
                <option value="premium">Premium — solicitudes ilimitadas y seguimiento</option>
            </select>
            <button type="submit">Abrir demostración</button>
        </form>
        <p class="intro">
            Accesos directos:
            <a href="/demo/normal">Normal</a> ·
            <a href="/demo/premium">Premium</a>
        </p>
        <a class="admin-link" href="/account/login?next=admin">
            Iniciar como administrador
        </a>
    </main>
</body>
</html>"""


def get_home_page(
    account_username=None,
    account_role="customer",
    form_error="",
    csrf_token="",
    company_plan="free",
    company_variant=DEFAULT_COMPANY_VARIANT,
):
    if account_username:
        admin_link_html = (
            '<a href="/admin/requests">Gestionar solicitudes</a>'
            if account_role == "admin" else ""
        )
        account_panel_html = f"""
        <div class="account-panel account-signed-in">
            <span>Cuenta: {escape(account_username)}</span>
            <a href="/dashboard">Mi panel</a>
            {admin_link_html}
            <a href="/account/logout">Cerrar sesión</a>
        </div>
        """
    else:
        account_panel_html = """
        <details class="account-panel">
            <summary>Iniciar sesión</summary>
            <form class="account-dropdown" action="/account/login" method="POST">
                <label for="home-username">Usuario</label>
                <input id="home-username" name="username" autocomplete="username" required>
                <label for="home-password">Contraseña</label>
                <input id="home-password" name="password" type="password"
                    autocomplete="current-password" required>
                <button type="submit">Iniciar sesión</button>
                <a class="create-account-link" href="/account/register">
                    ¿No tienes cuenta? Crea una
                </a>
            </form>
        </details>
        """
    form_error_html = (
        f'<p class="form-error" role="alert">{escape(form_error)}</p>'
        if form_error else ""
    )
    csrf_field_html = (
        f'<input type="hidden" name="csrf_token" value="{escape(csrf_token, quote=True)}">'
        if csrf_token else ""
    )
    plan_notice_html = (
        '<p class="plan-notice">Versión Premium: solicitudes ilimitadas y seguimiento '
        'de estados disponible. Inicia sesión antes de enviar para ver tus solicitudes.</p>'
        if company_plan == "pro" else
        '<p class="plan-notice">Versión Normal: hasta 30 solicitudes al mes. '
        'El seguimiento en línea está disponible en Premium.</p>'
    )

    # Generamos la lista de cotizaciones dinámicamente para que,
    # si cambias COTIZACIONES, la página se actualice automáticamente.
    cotizaciones_html = ""
    for index, item in enumerate(COTIZACIONES):
        # El precio numérico se usa para sumar y mostrar el total en el navegador.
        precio_en_colones = int(
            "".join(
                character
                for character in item["precio"]
                if character.isdigit()
            )
        )
        cotizaciones_html += f"""
            <label class="quote-item">
                <input
                    class="quote-checkbox"
                    type="checkbox"
                    name="products"
                    value="{index}"
                    data-price="{precio_en_colones}"
                    data-name="{escape(item["producto"], quote=True)}"
                >
                <span class="quote-name">{escape(item["producto"])}</span>
                <span class="quote-price">{escape(item["precio"])}</span>
            </label>
        """

    return (
    """
<!DOCTYPE html>

<html lang="es">

<head>

    <meta charset="UTF-8">

    <meta name="viewport"
          content="width=device-width, initial-scale=1.0">

    <title>Rocket & Voltic</title>


    <style>

        * {
            box-sizing: border-box;
            margin: 0;
            padding: 0;
        }


        body {

            min-height: 100vh;

            font-family:
                Arial,
                Helvetica,
                sans-serif;

            background:
                radial-gradient(
                    circle at top,
                    #06243d 0%,
                    #02070d 45%,
                    #000000 100%
                );

            color: white;

            display: flex;

            justify-content: center;

            align-items: center;

            padding: 30px;
        }


        .container {

            width: 100%;

            max-width: 700px;
        }


        .version-switch {
            display: flex;
            align-items: center;
            justify-content: center;
            flex-wrap: wrap;
            gap: 8px;
            margin-top: 14px;
            color: #b5c7d6;
            font-size: 13px;
        }
        .version-switch a { color: #8bcfff; }

        .account-panel {
            position: fixed;
            top: 20px;
            left: 20px;
            z-index: 2;
            width: 250px;
            color: white;
        }

        .account-panel summary {
            width: fit-content;
            padding: 12px 18px;
            border: 1px solid #0092fb;
            border-radius: 8px;
            background: #07111b;
            color: #8bcfff;
            font-weight: bold;
            cursor: pointer;
            list-style: none;
        }

        .account-panel summary::-webkit-details-marker {
            display: none;
        }

        .account-dropdown {
            margin-top: 8px;
            padding: 16px;
            border: 1px solid #12344d;
            border-radius: 10px;
            background: #07111b;
            box-shadow: 0 8px 24px rgba(0, 0, 0, .45);
        }

        .account-dropdown label {
            display: block;
            margin: 10px 0 6px;
            color: #dceeff;
        }

        .account-dropdown input {
            width: 100%;
            padding: 10px;
            border: 1px solid #24445c;
            border-radius: 6px;
            background: #02070d;
            color: white;
            font-size: 15px;
        }

        .account-dropdown button {
            width: 100%;
            margin-top: 14px;
            padding: 10px;
            border: 0;
            border-radius: 6px;
            background: #0092fb;
            color: white;
            font-weight: bold;
            cursor: pointer;
        }

        .account-dropdown .create-account-link {
            display: block;
            margin-top: 13px;
            color: #25D366;
            font-size: 13px;
            text-align: center;
        }

        .account-signed-in {
            display: grid;
            gap: 8px;
            padding: 12px 16px;
            border: 1px solid #12344d;
            border-radius: 8px;
            background: #07111b;
        }

        .account-signed-in a {
            color: #25D366;
            font-size: 14px;
        }


        /* =====================================================
           ENCABEZADO
           ===================================================== */

        .header {

            text-align: center;

            margin-bottom: 25px;
        }


        .logo-symbol {

            width: 90px;

            height: 90px;

            margin: 0 auto 15px auto;

            border-radius: 50%;

            border: 3px solid #0092fb;

            display: flex;

            justify-content: center;

            align-items: center;

            font-size: 42px;

            box-shadow:
                0 0 25px
                rgba(0, 146, 251, 0.35);
        }


        .title {

            font-size: 32px;

            font-weight: bold;

            color: white;

            letter-spacing: 1px;
        }


        .subtitle {

            margin-top: 8px;

            color: #0092fb;

            font-size: 15px;

            letter-spacing: 1px;
        }


        /* =====================================================
           TARJETA
           ===================================================== */

        .card {

            background:
                rgba(5, 12, 20, 0.96);

            border:
                1px solid #12344d;

            border-top:
                4px solid #0092fb;

            border-radius:
                14px;

            padding:
                35px;

            box-shadow:
                0 0 35px
                rgba(0, 146, 251, 0.12);
        }


        .card h2 {

            text-align: center;

            margin-bottom: 25px;

            font-size: 24px;
        }


        /* =====================================================
           CAMPOS
           ===================================================== */

        .form-group {

            margin-bottom: 20px;
        }


        label {

            display: block;

            margin-bottom: 8px;

            font-weight: bold;

            color: #dceeff;
        }


        input,
        textarea {

            width: 100%;

            padding: 14px;

            border-radius: 8px;

            border:
                1px solid #24445c;

            background:
                #07111b;

            color: white;

            font-size: 16px;

            outline: none;

            transition:
                0.2s;
        }


        input:focus,
        textarea:focus {

            border-color:
                #0092fb;

            box-shadow:
                0 0 10px
                rgba(0, 146, 251, 0.25);
        }


        textarea {

            min-height:
                160px;

            resize:
                vertical;
        }


        /* =====================================================
           BOTÓN
           ===================================================== */

        button {

            width: 100%;

            padding: 15px;

            border: none;

            border-radius: 8px;

            background:
                #0092fb;

            color: white;

            font-size: 17px;

            font-weight: bold;

            cursor: pointer;

            transition:
                0.2s;
        }


        button:hover {

            background:
                #007bd3;

            transform:
                translateY(-1px);

            box-shadow:
                0 5px 20px
                rgba(0, 146, 251, 0.25);
        }


        /* =====================================================
           INFORMACIÓN
           ===================================================== */

        .info {

            text-align: center;

            margin-top: 18px;

            color: #8fa8bb;

            font-size: 13px;

            line-height: 1.5;
        }


        .quote-section {

            margin-top: 28px;

            background:
                rgba(5, 12, 20, 0.96);

            border:
                1px solid #12344d;

            border-top:
                4px solid #25D366;

            border-radius:
                14px;

            padding:
                25px 20px;

            box-shadow:
                0 0 35px
                rgba(37, 211, 102, 0.08);
        }


        .quote-title {

            text-align: center;

            font-size: 22px;

            font-weight: bold;

            margin-bottom: 18px;

            color: #dceeff;
        }


        .quote-list {

            display: grid;

            gap: 12px;
        }


        .quote-item {

            display: flex;

            justify-content: space-between;

            align-items: center;

            gap: 10px;

            background:
                #07111b;

            border:
                1px solid #24445c;

            border-radius:
                10px;

            padding:
                14px 16px;

            cursor: pointer;
        }


        .quote-item:has(.quote-checkbox:checked) {

            border-color: #25D366;

            background: rgba(37, 211, 102, 0.08);
        }

        
        .quote-name {

            color: white;

            font-size: 15px;

            flex: 1;
        }


        .quote-price {

            color: #25D366;

            font-weight: bold;

            white-space: nowrap;
        }


        .quote-checkbox {

            width: 18px;

            height: 18px;

            accent-color: #25D366;

            cursor: pointer;
        }


        .selection-summary {

            margin-top: 18px;

            padding: 16px;

            background: #07111b;

            border: 1px solid #24445c;

            border-radius: 10px;
        }


        .selection-summary h3 {

            margin-bottom: 10px;

            color: #dceeff;

            font-size: 17px;
        }


        .selected-list {

            margin-bottom: 12px;

            padding-left: 20px;

            color: #b5c7d6;

            line-height: 1.6;
        }


        .selection-total {

            color: #25D366;

            font-size: 18px;

            font-weight: bold;
        }


        .whatsapp {

            color:
                #25D366;

            font-weight:
                bold;
        }


        /* =====================================================
           RESPONSIVE
           ===================================================== */

        @media (max-width: 600px) {

            body {

                padding:
                    15px;
            }


            .card {

                padding:
                    25px 20px;
            }


            .title {

                font-size:
                    26px;
            }


            .logo-symbol {

                width:
                    75px;

                height:
                    75px;

                font-size:
                    34px;
            }


            .account-panel {
                top: 12px;
                left: 12px;
                width: 220px;
            }

            .account-panel summary {
                padding: 10px 12px;
            }

        }

        .plan-notice {
            margin: 0 0 18px;
            padding: 11px 13px;
            border: 1px solid #24445c;
            border-radius: 8px;
            color: #b5c7d6;
            font-size: 13px;
            line-height: 1.5;
        }

        .form-error {
            margin: 0 0 16px;
            color: #ff8b8b;
            font-weight: bold;
        }

    </style>

</head>


<body>

    __ACCOUNT_PANEL__

    <div class="container">

        <!-- ==================================================
             ENCABEZADO
             ================================================== -->

        <div class="header">


            <div class="logo-symbol">
                🚀
            </div>


            <div class="title">
                Rocket & Voltic
            </div>


            <div class="subtitle">
                REPARACIÓN Y MANTENIMIENTO TECNOLÓGICO
            </div>

            __VERSION_SWITCH__


        </div>


        <!-- ==================================================
             FORMULARIO
             ================================================== -->

        <div class="card">


            <h2>
                Contáctanos
            </h2>

            __PLAN_NOTICE__
            __FORM_ERROR__

            <form
                id="contact-form"
                action="/send"
                method="POST"
            >

                __CSRF_FIELD__

                <!-- NOMBRE -->

                <div class="form-group">

                    <label for="name">
                        Nombre
                    </label>


                    <input
                        type="text"
                        id="name"
                        name="name"
                        placeholder="Escribe tu nombre por favor"
                        required
                    >

                </div>

                <div class="form-group">
                    <label for="contact">
                        Teléfono o correo de contacto
                    </label>
                    <input
                        type="text"
                        id="contact"
                        name="contact"
                        maxlength="160"
                        placeholder="¿Cómo podemos contactarte?"
                        required
                    >
                </div>


                <!-- MENSAJE -->

                <div class="form-group">

                    <label for="message">
                        Solicitud
                    </label>


                    <textarea
                        id="message"
                        name="message"
                        maxlength="4000"
                        placeholder="Escribe aquí tu solicitud por favor..."
                        required
                    ></textarea>

                </div>

                <!-- Cada casilla agrega o quita un producto del formulario. -->
                <div class="quote-section">

                    <div class="quote-title">
                        Cotizaciones
                    </div>

                    <div class="quote-list">
                        """ + cotizaciones_html + """
                    </div>

                    <!-- El resumen cambia en el momento en que se marca o desmarca un producto. -->
                    <div class="selection-summary" aria-live="polite">
                        <h3>Productos seleccionados</h3>
                        <ul class="selected-list" id="selected-products">
                            <li>No has seleccionado productos.</li>
                        </ul>
                        <div class="selection-total">
                            Total: <span id="selection-total">₡0</span>
                        </div>
                    </div>

                </div>


                <!-- BOTÓN -->

                <button type="submit">
                    Enviar mensaje
                </button>


            </form>

            <div class="info">

                Al enviar el formulario aparecerá una confirmación con un
                botón para abrir WhatsApp.


                <br>


                El mensaje quedará preparado
                para enviarlo.


                <br>


                Solo debes pulsar
                <strong>
                    Enviar
                </strong>.

            </div>


        </div>


    </div>

    <script>
        // Actualiza la lista y el monto cada vez que se marca o desmarca una casilla.
        const productCheckboxes = document.querySelectorAll(".quote-checkbox");
        const selectedProducts = document.getElementById("selected-products");
        const selectionTotal = document.getElementById("selection-total");

        function updateSelectionSummary() {
            const checkedProducts = Array.from(productCheckboxes)
                .filter((checkbox) => checkbox.checked);
            let total = 0;

            selectedProducts.replaceChildren();

            if (checkedProducts.length === 0) {
                const emptyMessage = document.createElement("li");
                emptyMessage.textContent = "No has seleccionado productos.";
                selectedProducts.appendChild(emptyMessage);
            } else {
                checkedProducts.forEach((checkbox) => {
                    const product = document.createElement("li");
                    const price = Number(checkbox.dataset.price);
                    product.textContent =
                        `${checkbox.dataset.name} — ${formatColones(price)}`;
                    selectedProducts.appendChild(product);
                    total += price;
                });
            }

            selectionTotal.textContent = formatColones(total);
        }

        function formatColones(amount) {
            return "₡" + new Intl.NumberFormat("es-CR").format(amount);
        }

        productCheckboxes.forEach((checkbox) => {
            checkbox.addEventListener("change", updateSelectionSummary);
        });
    </script>


</body>

</html>
""").replace("__ACCOUNT_PANEL__", account_panel_html).replace(
    "__PLAN_NOTICE__", plan_notice_html
).replace("__FORM_ERROR__", form_error_html).replace(
    "__CSRF_FIELD__", csrf_field_html
).replace(
    "__VERSION_SWITCH__", version_switch_html(company_variant)
)


def get_dashboard_page(
    account_username,
    account_role="customer",
    company_plan="free",
    company_variant=DEFAULT_COMPANY_VARIANT,
):
    admin_link_html = (
        '<a href="/admin/requests">Gestión de solicitudes</a>'
        if account_role == "admin" else ""
    )
    if company_plan == "pro":
        tracking_panel_html = """
            <section class="panel service-tracking" aria-labelledby="tracking-title">
                <div class="records-header">
                    <div>
                        <h2 id="tracking-title">Seguimiento de solicitudes</h2>
                        <p>Consulta el estado y las indicaciones del taller.</p>
                    </div>
                </div>
                <div class="request-list" id="customer-request-list" aria-live="polite">
                    <div class="loading-state">Cargando tus solicitudes…</div>
                </div>
            </section>
        """
        tracking_script = "loadCustomerRequests();"
    else:
        tracking_panel_html = """
            <section class="panel service-tracking plan-message">
                <div class="panel-heading">
                    <h2>Seguimiento de solicitudes</h2>
                    <p>La versión Normal acepta solicitudes, pero el seguimiento de estados
                       para clientes está disponible en Premium.</p>
                    <a class="secondary-button" href="/contact">Enviar una solicitud</a>
                </div>
            </section>
        """
        tracking_script = ""
    return """<!DOCTYPE html>
<html lang="es">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <meta name="theme-color" content="#07111b">
    <title>Mi panel | Rocket &amp; Voltic</title>
    <style>
        :root {
            color-scheme: dark;
            --page: #07111b;
            --surface: #0c1a27;
            --surface-raised: #102334;
            --border: #1c3a50;
            --text: #f2f7fb;
            --muted: #9bb0c0;
            --accent: #21a5ff;
            --accent-hover: #0788e7;
            --danger: #ff777e;
            --success: #61d99b;
        }
        * { box-sizing: border-box; }
        body {
            min-width: 320px;
            min-height: 100vh;
            margin: 0;
            background:
                radial-gradient(ellipse at 82% -10%, rgba(0, 146, 251, .18), transparent 42%),
                var(--page);
            color: var(--text);
            font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont,
                "Segoe UI", sans-serif;
        }
        a { color: inherit; }
        button, input, textarea { font: inherit; }
        button { cursor: pointer; }
        .shell { width: min(100% - 40px, 1080px); margin: 0 auto; }
        .topbar {
            display: flex;
            min-height: 76px;
            align-items: center;
            justify-content: space-between;
            gap: 20px;
            border-bottom: 1px solid rgba(155, 176, 192, .15);
        }
        .brand {
            display: inline-flex;
            align-items: center;
            gap: 11px;
            color: var(--text);
            font-weight: 750;
            letter-spacing: .01em;
            text-decoration: none;
        }
        .brand-mark {
            display: grid;
            width: 38px;
            height: 38px;
            place-items: center;
            border: 1px solid rgba(33, 165, 255, .4);
            border-radius: 12px;
            background: rgba(33, 165, 255, .12);
            font-size: 19px;
        }
        .top-actions { display: flex; align-items: center; gap: 16px; }
        .top-actions .version-switch {
            display: flex;
            align-items: center;
            gap: 7px;
            color: var(--muted);
            font-size: 12px;
        }
        .top-actions a {
            color: var(--muted);
            font-size: 14px;
            text-decoration: none;
        }
        .top-actions a:hover, .top-actions a:focus-visible { color: var(--text); }
        .top-actions .logout { color: #ffb1b5; }
        main { padding: clamp(32px, 6vw, 62px) 0 72px; }
        .eyebrow {
            margin: 0 0 10px;
            color: #73c5ff;
            font-size: 12px;
            font-weight: 750;
            letter-spacing: .16em;
            text-transform: uppercase;
        }
        .intro {
            display: flex;
            align-items: flex-end;
            justify-content: space-between;
            gap: 24px;
            margin-bottom: 34px;
        }
        h1 {
            margin: 0;
            font-size: clamp(30px, 5vw, 43px);
            letter-spacing: -.035em;
            line-height: 1.1;
        }
        .welcome { margin: 12px 0 0; color: var(--muted); line-height: 1.6; }
        .welcome strong { color: var(--text); font-weight: 650; }
        .summary {
            flex: 0 0 auto;
            min-width: 145px;
            padding: 17px 20px;
            border: 1px solid var(--border);
            border-radius: 14px;
            background: rgba(12, 26, 39, .8);
        }
        .summary-label { display: block; color: var(--muted); font-size: 12px; }
        .summary-count { display: block; margin-top: 5px; font-size: 27px; font-weight: 750; }
        .workspace {
            display: grid;
            grid-template-columns: minmax(260px, .78fr) minmax(0, 1.5fr);
            align-items: start;
            gap: 22px;
        }
        .panel {
            border: 1px solid var(--border);
            border-radius: 16px;
            background: linear-gradient(160deg, rgba(16, 35, 52, .96), rgba(10, 23, 35, .98));
            box-shadow: 0 18px 48px rgba(0, 0, 0, .15);
        }
        .panel-heading { padding: 22px 22px 0; }
        .panel-heading h2 { margin: 0; font-size: 18px; letter-spacing: -.01em; }
        .panel-heading p { margin: 8px 0 0; color: var(--muted); font-size: 13px; line-height: 1.5; }
        .record-form { display: grid; gap: 16px; padding: 20px 22px 22px; }
        .field { display: grid; gap: 8px; }
        label { color: #dce8f1; font-size: 13px; font-weight: 650; }
        input, textarea {
            width: 100%;
            border: 1px solid #29475d;
            border-radius: 9px;
            outline: none;
            background: #081520;
            color: var(--text);
            font-size: 15px;
            transition: border-color .16s, box-shadow .16s;
        }
        input { min-height: 46px; padding: 0 13px; }
        textarea { min-height: 132px; padding: 12px 13px; resize: vertical; line-height: 1.5; }
        input::placeholder, textarea::placeholder { color: #708698; }
        input:focus, textarea:focus {
            border-color: var(--accent);
            box-shadow: 0 0 0 3px rgba(33, 165, 255, .14);
        }
        .primary-button, .secondary-button, .danger-button {
            display: inline-flex;
            min-height: 42px;
            align-items: center;
            justify-content: center;
            gap: 8px;
            padding: 0 15px;
            border: 1px solid transparent;
            border-radius: 9px;
            font-size: 13px;
            font-weight: 700;
            text-decoration: none;
            transition: background .16s, border-color .16s, transform .16s;
        }
        .primary-button { background: var(--accent); color: #031421; }
        .primary-button:hover { background: #52b8ff; }
        .secondary-button { border-color: #31516a; background: #11283a; color: #d7e8f4; }
        .secondary-button:hover { border-color: #557a95; background: #173249; }
        .danger-button { border-color: rgba(255, 119, 126, .34); background: rgba(255, 119, 126, .08); color: #ff9ca2; }
        .danger-button:hover { background: rgba(255, 119, 126, .16); }
        button:focus-visible, a:focus-visible {
            outline: 3px solid rgba(87, 190, 255, .72);
            outline-offset: 3px;
        }
        button:disabled { cursor: wait; opacity: .65; }
        .feedback {
            margin: 0 22px 20px;
            padding: 11px 13px;
            border: 1px solid var(--border);
            border-radius: 9px;
            color: var(--muted);
            font-size: 13px;
            line-height: 1.5;
        }
        .feedback.error { border-color: rgba(255, 119, 126, .4); color: #ffb1b5; }
        .feedback.success { border-color: rgba(97, 217, 155, .36); color: #94edbd; }
        .records-panel { min-width: 0; }
        .service-tracking { margin-bottom: 22px; }
        .plan-message { padding-bottom: 22px; }
        .plan-message .secondary-button { margin-top: 16px; }
        .request-list { display: grid; gap: 12px; padding: 16px; }
        .request-card {
            padding: 18px;
            border: 1px solid rgba(66, 101, 126, .48);
            border-radius: 12px;
            background: rgba(5, 17, 27, .58);
        }
        .request-card-head {
            display: flex;
            align-items: flex-start;
            justify-content: space-between;
            gap: 14px;
        }
        .request-card h3 { margin: 0; font-size: 15px; }
        .request-description { margin: 9px 0 0; color: #b6c6d2; line-height: 1.55; white-space: pre-wrap; }
        .request-solution { margin-top: 13px; padding: 12px; border-radius: 9px; background: rgba(33, 165, 255, .08); color: #d7e8f4; line-height: 1.5; }
        .request-status { flex: 0 0 auto; padding: 6px 9px; border: 1px solid #31516a; border-radius: 999px; color: #9bd5ff; font-size: 11px; font-weight: 700; }
        .request-date { display: block; margin-top: 13px; color: #8299aa; font-size: 12px; }
        .records-header {
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 16px;
            padding: 22px;
            border-bottom: 1px solid rgba(155, 176, 192, .12);
        }
        .records-header h2 { margin: 0; font-size: 18px; }
        .records-header p { margin: 6px 0 0; color: var(--muted); font-size: 13px; }
        .record-list { display: grid; gap: 12px; padding: 16px; }
        .record-card {
            min-width: 0;
            padding: 18px;
            border: 1px solid rgba(66, 101, 126, .48);
            border-radius: 12px;
            background: rgba(5, 17, 27, .58);
        }
        .record-content { display: flex; align-items: flex-start; justify-content: space-between; gap: 18px; }
        .record-copy { min-width: 0; }
        .record-title { margin: 0; overflow-wrap: anywhere; font-size: 16px; font-weight: 700; }
        .record-description {
            margin: 9px 0 0;
            color: #b6c6d2;
            font-size: 14px;
            line-height: 1.6;
            overflow-wrap: anywhere;
            white-space: pre-wrap;
        }
        .record-date { display: block; margin-top: 14px; color: #8299aa; font-size: 12px; }
        .record-actions { display: flex; flex: 0 0 auto; gap: 8px; }
        .record-actions button { min-height: 36px; padding: 0 11px; }
        .empty-state, .loading-state {
            padding: 42px 22px;
            color: var(--muted);
            text-align: center;
            line-height: 1.6;
        }
        .empty-icon {
            display: grid;
            width: 48px;
            height: 48px;
            margin: 0 auto 13px;
            place-items: center;
            border: 1px solid var(--border);
            border-radius: 15px;
            background: rgba(33, 165, 255, .08);
            font-size: 22px;
        }
        .edit-form { display: grid; gap: 13px; }
        .edit-actions { display: flex; justify-content: flex-end; gap: 9px; }
        footer { padding: 0 0 28px; color: #718696; font-size: 12px; text-align: center; }
        [hidden] { display: none !important; }
        @media (max-width: 760px) {
            .workspace { grid-template-columns: 1fr; }
            .intro { align-items: flex-start; }
            .summary { min-width: 120px; }
        }
        @media (max-width: 520px) {
            .shell { width: min(100% - 28px, 1080px); }
            .topbar { min-height: 66px; flex-wrap: wrap; padding: 12px 0; }
            .top-actions { gap: 12px; flex-wrap: wrap; justify-content: center; }
            .top-actions a { font-size: 12px; }
            .top-actions .home-link { display: none; }
            main { padding-top: 34px; }
            .intro { flex-direction: column; gap: 18px; margin-bottom: 22px; }
            .summary { width: 100%; padding: 13px 16px; }
            .summary-count { font-size: 23px; }
            .record-content { flex-direction: column; }
            .record-actions { width: 100%; }
            .record-actions button { flex: 1; }
            .panel-heading, .records-header { padding-right: 17px; padding-left: 17px; }
            .record-form { padding-right: 17px; padding-left: 17px; }
            .feedback { margin-right: 17px; margin-left: 17px; }
            .record-list { padding: 11px; }
            .record-card { padding: 15px; }
        }
        @media (prefers-reduced-motion: reduce) {
            *, *::before, *::after { scroll-behavior: auto !important; transition-duration: .01ms !important; }
        }
    </style>
</head>
<body>
    <div class="shell">
        <header class="topbar">
            <a class="brand" href="/" aria-label="Rocket &amp; Voltic, inicio">
                <span class="brand-mark" aria-hidden="true">🚀</span>
                <span>Rocket &amp; Voltic</span>
            </a>
            <nav class="top-actions" aria-label="Navegación de cuenta">
                <a class="home-link" href="/">Inicio</a>
                __VERSION_SWITCH__
                __ADMIN_LINK__
                <a href="/contact">Servicios</a>
                <a class="logout" href="/account/logout">Cerrar sesión</a>
            </nav>
        </header>

        <main>
            <section class="intro" aria-labelledby="page-title">
                <div>
                    <p class="eyebrow">Espacio personal</p>
                    <h1 id="page-title">Tu panel de control</h1>
                    <p class="welcome">Hola, <strong>__ACCOUNT_USERNAME__</strong>. Consulta tus solicitudes y conserva notas privadas.</p>
                </div>
                <div class="summary" aria-live="polite">
                    <span class="summary-label">Notas guardadas</span>
                    <span class="summary-count" id="record-count">—</span>
                </div>
            </section>

            __SERVICE_TRACKING_PANEL__

            <div class="workspace">
                <section class="panel" aria-labelledby="create-title">
                    <div class="panel-heading">
                        <h2 id="create-title">Nueva nota</h2>
                        <p>Guarda información personal que no se envía al taller.</p>
                    </div>
                    <form class="record-form" id="create-form">
                        <div class="field">
                            <label for="record-title">Título</label>
                            <input id="record-title" name="title" maxlength="120"
                                placeholder="Ej. Información para recordar" required>
                        </div>
                        <div class="field">
                            <label for="record-description">Descripción</label>
                            <textarea id="record-description" name="description" maxlength="4000"
                                placeholder="Agrega detalles para esta nota..."></textarea>
                        </div>
                        <button class="primary-button" id="create-button" type="submit">
                            <span aria-hidden="true">＋</span> Guardar nota
                        </button>
                    </form>
                    <p class="feedback" id="feedback" role="status" aria-live="polite" hidden></p>
                </section>

                <section class="panel records-panel" aria-labelledby="records-title">
                    <div class="records-header">
                        <div>
                            <h2 id="records-title">Mis notas</h2>
                            <p>Son privadas y no forman parte de tus solicitudes de servicio.</p>
                        </div>
                    </div>
                    <div class="record-list" id="record-list" aria-live="polite">
                        <div class="loading-state">Cargando tus registros…</div>
                    </div>
                </section>
            </div>
        </main>
        <footer>Tu información se guarda de forma privada en tu cuenta.</footer>
    </div>

    <script>
        const recordList = document.getElementById("record-list");
        const recordCount = document.getElementById("record-count");
        const feedback = document.getElementById("feedback");
        const createForm = document.getElementById("create-form");

        function getCsrfToken() {
            const cookie = document.cookie
                .split("; ")
                .find((item) => item.startsWith("rv_user_csrf="));
            return cookie ? decodeURIComponent(cookie.slice("rv_user_csrf=".length)) : "";
        }

        async function apiRequest(path, options = {}) {
            const headers = { ...(options.headers || {}) };
            const method = options.method || "GET";
            if (options.body) {
                headers["Content-Type"] = "application/json";
            }
            if (method !== "GET") {
                headers["X-CSRF-Token"] = getCsrfToken();
            }
            const response = await fetch(path, {
                ...options,
                headers,
                credentials: "same-origin"
            });
            let payload = {};
            try {
                payload = await response.json();
            } catch {
                if (response.ok) {
                    throw new Error("El servidor devolvió una respuesta inválida.");
                }
            }
            if (response.status === 401) {
                window.location.assign("/account/login");
                throw new Error("Tu sesión terminó. Inicia sesión nuevamente.");
            }
            if (!response.ok) {
                throw new Error(payload.error || "No se pudo completar la solicitud.");
            }
            return payload;
        }

        function showFeedback(message, kind = "") {
            feedback.textContent = message;
            feedback.className = "feedback" + (kind ? " " + kind : "");
            feedback.hidden = !message;
        }

        function makeButton(label, className, action) {
            const button = document.createElement("button");
            button.type = "button";
            button.className = className;
            button.textContent = label;
            button.addEventListener("click", action);
            return button;
        }

        function displayDate(value) {
            if (!value) return "";
            const parsed = new Date(value.replace(" ", "T") + "Z");
            return Number.isNaN(parsed.getTime())
                ? value
                : "Actualizado " + parsed.toLocaleString("es-CR");
        }

        function renderRecords(records) {
            recordCount.textContent = new Intl.NumberFormat("es-CR").format(records.length);
            recordList.replaceChildren();
            if (records.length === 0) {
                const empty = document.createElement("div");
                empty.className = "empty-state";
                const icon = document.createElement("div");
                icon.className = "empty-icon";
                icon.setAttribute("aria-hidden", "true");
                icon.textContent = "✦";
                const message = document.createElement("div");
                message.textContent = "Aún no tienes notas. Crea la primera con el formulario.";
                empty.append(icon, message);
                recordList.appendChild(empty);
                return;
            }
            records.forEach((record) => recordList.appendChild(makeRecordCard(record)));
        }

        function makeRecordCard(record) {
            const card = document.createElement("article");
            card.className = "record-card";
            const content = document.createElement("div");
            content.className = "record-content";
            const copy = document.createElement("div");
            copy.className = "record-copy";
            const title = document.createElement("h3");
            title.className = "record-title";
            title.textContent = record.title;
            copy.appendChild(title);
            if (record.description) {
                const description = document.createElement("p");
                description.className = "record-description";
                description.textContent = record.description;
                copy.appendChild(description);
            }
            const date = document.createElement("time");
            date.className = "record-date";
            date.dateTime = record.updated_at.replace(" ", "T") + "Z";
            date.textContent = displayDate(record.updated_at);
            copy.appendChild(date);

            const actions = document.createElement("div");
            actions.className = "record-actions";
            actions.append(
                makeButton("Editar", "secondary-button", () => editRecord(card, record)),
                makeButton("Eliminar", "danger-button", () => removeRecord(record))
            );
            content.append(copy, actions);
            card.appendChild(content);
            return card;
        }

        function addEditField(form, labelText, value, multiline) {
            const field = document.createElement("div");
            field.className = "field";
            const label = document.createElement("label");
            const input = document.createElement(multiline ? "textarea" : "input");
            input.name = multiline ? "description" : "title";
            input.maxLength = multiline ? 4000 : 120;
            input.value = value || "";
            if (!multiline) input.required = true;
            label.textContent = labelText;
            label.htmlFor = "edit-" + input.name;
            input.id = label.htmlFor;
            field.append(label, input);
            form.appendChild(field);
            return input;
        }

        function editRecord(card, record) {
            const form = document.createElement("form");
            form.className = "edit-form";
            const title = addEditField(form, "Título", record.title, false);
            const description = addEditField(form, "Descripción", record.description, true);
            const actions = document.createElement("div");
            actions.className = "edit-actions";
            const cancel = makeButton("Cancelar", "secondary-button", loadRecords);
            cancel.type = "button";
            const save = document.createElement("button");
            save.className = "primary-button";
            save.type = "submit";
            save.textContent = "Guardar cambios";
            actions.append(cancel, save);
            form.appendChild(actions);
            form.addEventListener("submit", async (event) => {
                event.preventDefault();
                save.disabled = true;
                try {
                    await apiRequest("/api/records/" + record.id, {
                        method: "PUT",
                        body: JSON.stringify({ title: title.value, description: description.value })
                    });
                    showFeedback("Los cambios de la nota se guardaron correctamente.", "success");
                    await loadRecords();
                } catch (error) {
                    showFeedback(error.message, "error");
                    save.disabled = false;
                }
            });
            card.replaceChildren(form);
            title.focus();
        }

        async function removeRecord(record) {
            if (!window.confirm("¿Eliminar el registro “" + record.title + "”? Esta acción no se puede deshacer.")) {
                return;
            }
            try {
                await apiRequest("/api/records/" + record.id, { method: "DELETE" });
                showFeedback("La nota se eliminó.", "success");
                await loadRecords();
            } catch (error) {
                showFeedback(error.message, "error");
            }
        }

        async function loadRecords() {
            recordList.replaceChildren();
            const loading = document.createElement("div");
            loading.className = "loading-state";
            loading.textContent = "Cargando tus registros…";
            recordList.appendChild(loading);
            try {
                const payload = await apiRequest("/api/records");
                if (!Array.isArray(payload.records)) {
                    throw new Error("El servidor devolvió una lista de registros inválida.");
                }
                renderRecords(payload.records);
            } catch (error) {
                recordCount.textContent = "—";
                const message = document.createElement("div");
                message.className = "empty-state";
                message.textContent = error.message;
                recordList.replaceChildren(message);
                showFeedback(error.message, "error");
            }
        }

        createForm.addEventListener("submit", async (event) => {
            event.preventDefault();
            const button = document.getElementById("create-button");
            const values = new FormData(createForm);
            button.disabled = true;
            try {
                await apiRequest("/api/records", {
                    method: "POST",
                    body: JSON.stringify({
                        title: values.get("title"),
                        description: values.get("description")
                    })
                });
                createForm.reset();
                showFeedback("La nota se creó correctamente.", "success");
                await loadRecords();
            } catch (error) {
                showFeedback(error.message, "error");
            } finally {
                button.disabled = false;
            }
        });

        async function loadCustomerRequests() {
            const container = document.getElementById("customer-request-list");
            if (!container) return;
            try {
                const payload = await apiRequest("/api/customer/requests");
                if (!Array.isArray(payload.requests)) {
                    throw new Error("El servidor devolvió una lista de solicitudes inválida.");
                }
                container.replaceChildren();
                if (payload.requests.length === 0) {
                    const empty = document.createElement("div");
                    empty.className = "empty-state";
                    empty.textContent = "Todavía no hay solicitudes asociadas a tu cuenta. Inicia sesión antes de enviar una para poder seguirla aquí.";
                    container.appendChild(empty);
                    return;
                }
                payload.requests.forEach((request) => {
                    const card = document.createElement("article");
                    card.className = "request-card";
                    const head = document.createElement("div");
                    head.className = "request-card-head";
                    const heading = document.createElement("h3");
                    heading.textContent = "Solicitud #" + request.id;
                    const status = document.createElement("span");
                    status.className = "request-status";
                    status.textContent = request.status_label;
                    head.append(heading, status);
                    const description = document.createElement("p");
                    description.className = "request-description";
                    description.textContent = request.description;
                    card.append(head, description);
                    if (request.solution) {
                        const solution = document.createElement("p");
                        solution.className = "request-solution";
                        solution.textContent = "Nota del taller: " + request.solution;
                        card.appendChild(solution);
                    }
                    const date = document.createElement("time");
                    date.className = "request-date";
                    date.textContent = "Actualizada " +
                        new Date(request.updated_at.replace(" ", "T") + "Z")
                            .toLocaleString("es-CR");
                    card.appendChild(date);
                    container.appendChild(card);
                });
            } catch (error) {
                const message = document.createElement("div");
                message.className = "empty-state";
                message.textContent = error.message;
                container.replaceChildren(message);
            }
        }

        loadRecords();
        __CUSTOMER_TRACKING_SCRIPT__
    </script>
</body>
</html>""".replace("__ACCOUNT_USERNAME__", escape(account_username)).replace(
    "__ADMIN_LINK__", admin_link_html
).replace("__SERVICE_TRACKING_PANEL__", tracking_panel_html).replace(
    "__CUSTOMER_TRACKING_SCRIPT__", tracking_script
).replace(
    "__VERSION_SWITCH__", version_switch_html(company_variant)
)


def get_admin_requests_page(
    admin_username,
    company_plan="free",
    company_variant=DEFAULT_COMPANY_VARIANT,
):
    return """<!DOCTYPE html>
<html lang="es">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <meta name="theme-color" content="#07111b">
    <title>Gestión de solicitudes | Rocket &amp; Voltic</title>
    <style>
        * { box-sizing: border-box; }
        body {
            min-height: 100vh;
            margin: 0;
            background: radial-gradient(ellipse at 80% -10%, rgba(0, 146, 251, .18), transparent 42%), #07111b;
            color: #f2f7fb;
            font-family: Inter, ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif;
        }
        .shell { width: min(100% - 36px, 1050px); margin: 0 auto; }
        header {
            display: flex;
            min-height: 72px;
            align-items: center;
            justify-content: space-between;
            gap: 16px;
            border-bottom: 1px solid rgba(155, 176, 192, .15);
        }
        header a { color: #b7c9d6; font-size: 13px; text-decoration: none; }
        header a:hover { color: #fff; }
        nav { display: flex; align-items: center; gap: 18px; }
        main { padding: clamp(30px, 6vw, 58px) 0 70px; }
        .eyebrow { margin: 0 0 9px; color: #73c5ff; font-size: 12px; font-weight: 750; letter-spacing: .15em; text-transform: uppercase; }
        h1 { margin: 0; font-size: clamp(28px, 5vw, 40px); letter-spacing: -.03em; }
        .intro { margin: 10px 0 22px; color: #9bb0c0; line-height: 1.55; }
        .admin-toolbar {
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 14px;
            margin: 18px 0;
        }
        .active-version { margin: 0; color: #b9cad6; font-size: 14px; }
        .switch-version-button {
            display: inline-flex;
            min-height: 42px;
            align-items: center;
            justify-content: center;
            padding: 0 15px;
            border-radius: 8px;
            background: #21a5ff;
            color: #031421;
            font-size: 13px;
            font-weight: 750;
            text-decoration: none;
        }
        .switch-version-button:hover { background: #52b8ff; }
        .quota, .feedback {
            margin: 18px 0;
            padding: 13px 15px;
            border: 1px solid #1c3a50;
            border-radius: 10px;
            background: #0c1a27;
            color: #b9cad6;
            font-size: 13px;
        }
        .feedback:empty { display: none; }
        .feedback.error { border-color: rgba(255, 119, 126, .4); color: #ffb1b5; }
        .request-list { display: grid; gap: 14px; }
        .request-card {
            padding: 20px;
            border: 1px solid #1c3a50;
            border-radius: 14px;
            background: linear-gradient(160deg, #102334, #0c1a27);
        }
        .request-head { display: flex; justify-content: space-between; gap: 16px; }
        h2 { margin: 0; font-size: 17px; }
        .request-meta { margin: 7px 0 0; color: #9bb0c0; font-size: 13px; overflow-wrap: anywhere; }
        .request-status { flex: 0 0 auto; color: #9bd5ff; font-size: 12px; font-weight: 700; }
        .request-description { margin: 16px 0 9px; line-height: 1.6; white-space: pre-wrap; overflow-wrap: anywhere; }
        .products { margin: 8px 0; color: #9bb0c0; font-size: 13px; line-height: 1.55; }
        .total { color: #7ee6a5; font-weight: 700; }
        .controls { display: grid; grid-template-columns: minmax(180px, .7fr) minmax(0, 1.3fr) auto; align-items: end; gap: 12px; margin-top: 18px; }
        label { display: grid; gap: 7px; color: #dce8f1; font-size: 12px; font-weight: 650; }
        select, textarea {
            width: 100%;
            border: 1px solid #29475d;
            border-radius: 8px;
            background: #081520;
            color: #f2f7fb;
            font: inherit;
        }
        select { min-height: 42px; padding: 0 10px; }
        textarea { min-height: 42px; padding: 10px; resize: vertical; }
        select:focus, textarea:focus { outline: 2px solid rgba(33, 165, 255, .55); outline-offset: 1px; }
        button {
            min-height: 42px;
            padding: 0 15px;
            border: 0;
            border-radius: 8px;
            background: #21a5ff;
            color: #031421;
            font: inherit;
            font-size: 13px;
            font-weight: 750;
            cursor: pointer;
        }
        button:hover { background: #52b8ff; }
        button:disabled { cursor: wait; opacity: .65; }
        .empty { padding: 48px 20px; border: 1px solid #1c3a50; border-radius: 14px; color: #9bb0c0; text-align: center; }
        [hidden] { display: none !important; }
        @media (max-width: 700px) {
            .admin-toolbar { align-items: flex-start; flex-direction: column; }
            .controls { grid-template-columns: 1fr; }
            .request-head { flex-direction: column; gap: 7px; }
            .request-status { order: -1; }
        }
        @media (max-width: 440px) {
            .shell { width: min(100% - 26px, 1050px); }
            header { min-height: 64px; flex-wrap: wrap; justify-content: center; padding: 10px 0; }
            nav { gap: 11px; flex-wrap: wrap; justify-content: center; }
            .version-switch span { display: none; }
            nav a { font-size: 12px; }
            .request-card { padding: 16px; }
        }
    </style>
</head>
<body>
    <div class="shell">
        <header>
            <a href="/">Rocket &amp; Voltic · __ADMIN_USERNAME__</a>
            <nav aria-label="Navegación">
                <a href="/dashboard">Mi panel</a>
                <a href="/account/logout">Cerrar sesión</a>
            </nav>
        </header>
        <main>
            <p class="eyebrow">Panel de la empresa</p>
            <h1>Solicitudes de servicio</h1>
            <p class="intro">__PLAN_DESCRIPTION__ Administra las solicitudes de tus clientes, actualiza el estado y registra la solución.</p>
            <div class="admin-toolbar">
                <p class="active-version">Versión actual: __VERSION_LABEL__</p>
                <a class="switch-version-button" href="/admin/switch-version">
                    Cambiar de versión
                </a>
            </div>
            <div class="quota" id="quota">Cargando uso mensual…</div>
            <p class="feedback" id="feedback" role="status" aria-live="polite"></p>
            <section id="request-list" class="request-list" aria-live="polite">
                <div class="empty">Cargando solicitudes…</div>
            </section>
        </main>
    </div>
    <script>
        const requestList = document.getElementById("request-list");
        const feedback = document.getElementById("feedback");
        const quota = document.getElementById("quota");
        const statusLabels = {
            pending: "En espera de aceptación",
            accepted: "Aceptada",
            denied: "Denegada",
            in_progress: "En trabajo",
            completed: "Terminada"
        };

        function csrfToken() {
            const cookie = document.cookie.split("; ").find(item => item.startsWith("rv_user_csrf="));
            return cookie ? decodeURIComponent(cookie.slice("rv_user_csrf=".length)) : "";
        }

        async function api(path, options = {}) {
            const headers = { ...(options.headers || {}) };
            const method = options.method || "GET";
            if (options.body) headers["Content-Type"] = "application/json";
            if (method !== "GET") headers["X-CSRF-Token"] = csrfToken();
            const response = await fetch(path, { ...options, headers, credentials: "same-origin" });
            let payload = {};
            try {
                payload = await response.json();
            } catch {
                if (response.ok) throw new Error("Respuesta inválida del servidor.");
            }
            if (response.status === 401) {
                window.location.assign("/account/login");
                throw new Error("Inicia sesión nuevamente.");
            }
            if (!response.ok) throw new Error(payload.error || "No se pudo completar la solicitud.");
            return payload;
        }

        function setFeedback(message, error = false) {
            feedback.textContent = message;
            feedback.className = "feedback" + (error ? " error" : "");
        }

        function renderRequests(requests) {
            requestList.replaceChildren();
            if (requests.length === 0) {
                const empty = document.createElement("div");
                empty.className = "empty";
                empty.textContent = "Todavía no hay solicitudes recibidas.";
                requestList.appendChild(empty);
                return;
            }
            requests.forEach(request => {
                const card = document.createElement("article");
                card.className = "request-card";
                const head = document.createElement("div");
                head.className = "request-head";
                const customer = document.createElement("div");
                const title = document.createElement("h2");
                title.textContent = "Solicitud #" + request.id + " · " + request.customer_name;
                const contact = document.createElement("p");
                contact.className = "request-meta";
                contact.textContent = request.customer_contact;
                customer.append(title, contact);
                const statusLabel = document.createElement("span");
                statusLabel.className = "request-status";
                statusLabel.textContent = statusLabels[request.status] || request.status_label;
                head.append(customer, statusLabel);
                const description = document.createElement("p");
                description.className = "request-description";
                description.textContent = request.description;
                card.append(head, description);
                if (request.selected_products.length) {
                    const products = document.createElement("p");
                    products.className = "products";
                    products.textContent = "Servicios: " + request.selected_products
                        .map(item => item.producto + " (" + item.precio + ")").join(", ");
                    card.appendChild(products);
                }
                const total = document.createElement("p");
                total.className = "products total";
                total.textContent = "Total de referencia: ₡" +
                    new Intl.NumberFormat("es-CR").format(request.quoted_total);
                card.appendChild(total);

                const controls = document.createElement("form");
                controls.className = "controls";
                const statusField = document.createElement("label");
                statusField.textContent = "Estado";
                const select = document.createElement("select");
                select.name = "status";
                Object.entries(statusLabels).forEach(([value, label]) => {
                    const option = document.createElement("option");
                    option.value = value;
                    option.textContent = label;
                    option.selected = value === request.status;
                    select.appendChild(option);
                });
                statusField.appendChild(select);
                const solutionField = document.createElement("label");
                solutionField.textContent = "Respuesta o solución";
                const solution = document.createElement("textarea");
                solution.name = "solution";
                solution.maxLength = 4000;
                solution.value = request.solution;
                solution.placeholder = "Motivo de rechazo, indicaciones o solución aplicada";
                solutionField.appendChild(solution);
                const save = document.createElement("button");
                save.type = "submit";
                save.textContent = "Guardar estado";
                controls.append(statusField, solutionField, save);
                controls.addEventListener("submit", async event => {
                    event.preventDefault();
                    save.disabled = true;
                    try {
                        await api("/api/admin/requests/" + request.id, {
                            method: "PUT",
                            body: JSON.stringify({ status: select.value, solution: solution.value })
                        });
                        setFeedback("Solicitud #" + request.id + " actualizada.");
                        await loadRequests();
                    } catch (error) {
                        setFeedback(error.message, true);
                        save.disabled = false;
                    }
                });
                card.appendChild(controls);
                requestList.appendChild(card);
            });
        }

        async function loadRequests() {
            try {
                const payload = await api("/api/admin/requests");
                quota.textContent = payload.plan === "free"
                    ? "Versión Normal · " + payload.monthly_count + " de " +
                        payload.monthly_limit + " solicitudes recibidas este mes"
                    : "Versión Premium · solicitudes ilimitadas · " +
                        payload.monthly_count + " recibidas este mes";
                renderRequests(payload.requests);
            } catch (error) {
                setFeedback(error.message, true);
                const empty = document.createElement("div");
                empty.className = "empty";
                empty.textContent = error.message;
                requestList.replaceChildren(empty);
            }
        }

        loadRequests();
    </script>
</body>
</html>""".replace("__ADMIN_USERNAME__", escape(admin_username)).replace(
        "__VERSION_LABEL__", escape(COMPANY_VARIANTS[company_variant]["label"])
    ).replace(
        "__PLAN_DESCRIPTION__",
        "Plan Normal: máximo 30 solicitudes al mes."
        if company_plan == "free"
        else "Plan Premium: solicitudes ilimitadas y seguimiento de clientes."
    )


def get_account_page(
    mode,
    error_message="",
    success_message="",
    admin_login=False,
):
    is_registration = mode == "register"
    title = (
        "Crear cuenta"
        if is_registration
        else "Iniciar sesión como administrador"
        if admin_login
        else "Iniciar sesión"
    )
    action = (
        "/account/register"
        if is_registration
        else "/account/login?next=admin"
        if admin_login
        else "/account/login"
    )
    submit_label = "Crear cuenta" if is_registration else "Iniciar sesión"
    intro = (
        "Accede al panel para gestionar las solicitudes de servicio."
        if admin_login else
        "Esta cuenta te permite enviar solicitudes y consultar su avance en la versión Premium."
    )
    error_html = (
        f'<p class="error" role="alert">{escape(error_message)}</p>'
        if error_message else ""
    )
    success_html = (
        f'<p class="success" role="status">{escape(success_message)}</p>'
        if success_message else ""
    )
    confirmation_field = """
        <label for="password-confirm">Confirma la contraseña</label>
        <input id="password-confirm" name="password_confirm" type="password"
            autocomplete="new-password" required>
    """ if is_registration else ""
    alternate_link = (
        '<a href="/account/login">¿Ya tienes cuenta? Inicia sesión</a>'
        if is_registration else
        '<a href="/account/register">¿No tienes cuenta? Crea una</a>'
    )
    password_autocomplete = "new-password" if is_registration else "current-password"
    return f"""<!DOCTYPE html>
<html lang="es">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{title} | Rocket & Voltic</title>
    <style>
        * {{ box-sizing: border-box; }}
        body {{ min-height: 100vh; margin: 0; padding: 24px; display: grid;
            place-items: center; background: #02070d; color: white;
            font-family: Arial, Helvetica, sans-serif; }}
        main {{ width: 100%; max-width: 430px; padding: 30px;
            background: #07111b; border: 1px solid #12344d;
            border-top: 4px solid #0092fb; border-radius: 14px; }}
        h1 {{ margin-top: 0; text-align: center; }}
        p {{ color: #b5c7d6; line-height: 1.5; }}
        label {{ display: block; margin: 18px 0 7px; color: #dceeff; }}
        input {{ width: 100%; padding: 13px; border: 1px solid #24445c;
            border-radius: 8px; background: #02070d; color: white; font-size: 16px; }}
        button {{ width: 100%; margin-top: 22px; padding: 14px; border: 0;
            border-radius: 8px; background: #0092fb; color: white;
            font-size: 16px; font-weight: bold; cursor: pointer; }}
        a {{ display: block; margin-top: 18px; color: #25D366; text-align: center; }}
        .error {{ color: #ff8b8b; }}
        .success {{ color: #7ee6a5; }}
    </style>
</head>
<body>
    <main>
        <h1>{title}</h1>
        <p>{intro}</p>
        {error_html}
        {success_html}
        <form action="{action}" method="POST">
            <label for="username">Usuario</label>
            <input id="username" name="username" autocomplete="username" required>
            <label for="password">Contraseña</label>
            <input id="password" name="password" type="password"
                autocomplete="{password_autocomplete}" required>
            {confirmation_field}
            <button type="submit">{submit_label}</button>
        </form>
        {alternate_link}
        <a href="/">Volver al inicio</a>
    </main>
</body>
</html>"""


# ============================================================
# SERVIDOR
# ============================================================

class RocketAndVoltic(BaseHTTPRequestHandler):
    server_version = "RocketAndVoltic"
    sys_version = ""

    def end_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline'; connect-src 'self'; "
            "img-src 'self' data:; form-action 'self'; "
            "frame-ancestors 'none'; base-uri 'self'",
        )
        if COOKIE_SECURE:
            self.send_header("Strict-Transport-Security", "max-age=31536000")
        super().end_headers()


    # ========================================================
    # GET
    # ========================================================

    def do_GET(self):

        path = self.path.split("?", 1)[0]

        if path == "/healthz":
            self.send_json(200, {"status": "ok"})

        elif path == "/api/records" or path.startswith("/api/records/"):
            self.handle_user_records_request("GET", path)

        elif path == "/api/customer/requests":
            self.handle_customer_requests_request()

        elif path == "/api/admin/requests":
            self.handle_admin_requests_request("GET", None)

        elif path == "/":
            if self.get_selected_company():
                self.show_landing()
            else:
                self.send_html(200, get_company_selection_page())

        elif path == "/select-company":
            self.send_html(
                200,
                get_company_selection_page(),
                cookie=(
                    "rv_company=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0"
                    f"{SECURE_COOKIE_ATTRIBUTE}"
                ),
            )

        elif path == "/demo/normal":
            self.select_company_variant("normal")

        elif path == "/demo/premium":
            self.select_company_variant("premium")

        elif path == "/contact":

            self.show_contact()

        elif path == "/dashboard":
            session = self.get_user_session()
            if session is None:
                self.send_redirect("/account/login")
            else:
                company_variant = self.get_company_variant()
                self.send_html(
                    200,
                    get_dashboard_page(
                        session[1],
                        session[3],
                        company_plan_for_variant(company_variant),
                        company_variant,
                    ),
                )

        elif path == "/admin/switch-version":
            session = self.get_user_session()
            if session is None:
                self.send_redirect("/account/login?next=admin")
            elif session[3] != "admin":
                self.send_html(403, "<h1>Acceso restringido</h1>")
            else:
                current_variant = self.get_company_variant()
                next_variant = (
                    "premium" if current_variant == "normal" else "normal"
                )
                self.select_company_variant(
                    next_variant,
                    return_to="/admin/requests",
                )

        elif path == "/admin/requests":
            session = self.get_user_session()
            if session is None:
                self.send_redirect("/account/login")
            elif session[3] != "admin":
                self.send_html(403, "<h1>Acceso restringido</h1>")
            else:
                company_variant = self.get_company_variant()
                self.send_html(
                    200,
                    get_admin_requests_page(
                        session[1],
                        company_plan_for_variant(company_variant),
                        company_variant,
                    ),
                )

        elif path == "/account/login":
            query = parse_qs(self.path.partition("?")[2])
            success_message = (
                "Tu cuenta se creó. Ahora inicia sesión."
                if query.get("created") == ["1"]
                else ""
            )
            self.send_html(
                200,
                get_account_page(
                    "login",
                    success_message=success_message,
                    admin_login=query.get("next") == ["admin"],
                ),
            )

        elif path == "/account/register":
            self.send_html(200, get_account_page("register"))

        elif path == "/account/logout":
            self.logout_user()

        else:

            self.send_error(
                404,
                "Página no encontrada"
            )


    # ========================================================
    # POST
    # ========================================================

    def do_POST(self):

        path = self.path.split("?", 1)[0]

        if path == "/api/records" or path.startswith("/api/records/"):
            self.handle_user_records_request("POST", path)
            return

        if path == "/select-company":
            self.select_company()
            return

        if path == "/account/login":
            self.login_user()
            return

        if path == "/account/register":
            self.register_user_account()
            return

        if path != "/send":

            self.send_error(
                404,
                "Página no encontrada"
            )

            return

        company_variant = self.get_company_variant()
        company_plan = company_plan_for_variant(company_variant)

        # ====================================================
        # LEER DATOS DEL FORMULARIO
        # ====================================================

        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self.send_error(400, "Longitud de solicitud inválida")
            return

        if content_length <= 0 or content_length > 20000:
            self.send_error(413, "La solicitud está vacía o es demasiado grande")
            return

        post_data = self.rfile.read(content_length)


        # ====================================================
        # CONVERTIR DATOS
        # ====================================================

        try:
            form_data = parse_qs(post_data.decode("utf-8"))
        except UnicodeDecodeError:
            self.send_error(400, "El formulario no tiene un formato válido")
            return


        name = form_data.get("name", [""])[0].strip()
        customer_contact = form_data.get("contact", [""])[0].strip()
        message = form_data.get("message", [""])[0].strip()
        session = self.get_user_session()
        csrf_token = form_data.get("csrf_token", [""])[0]
        if session is not None and not self.is_user_csrf_valid(session, csrf_token):
            self.send_html(
                403,
                get_home_page(
                    session[1],
                    session[3],
                    form_error="La validación de seguridad falló. Recarga la página e inténtalo otra vez.",
                    company_plan=company_plan,
                    company_variant=company_variant,
                ),
            )
            return
        if (
            not name
            or len(name) > 100
            or not customer_contact
            or len(customer_contact) > 160
            or not message
            or len(message) > MAX_RECORD_DESCRIPTION_LENGTH
        ):
            self.send_html(
                400,
                get_home_page(
                    session[1] if session else None,
                    session[3] if session else "customer",
                    form_error="Completa los datos de contacto y una solicitud válida.",
                    csrf_token=csrf_token,
                    company_plan=company_plan,
                    company_variant=company_variant,
                ),
            )
            return

        # El servidor vuelve a obtener productos y precios desde COTIZACIONES.
        # Así, el mensaje de WhatsApp no depende de precios enviados por el navegador.
        selected_product_indexes = {
            int(value)
            for value in form_data.get("products", [])
            if value.isdigit() and int(value) < len(COTIZACIONES)
        }
        selected_products = [
            item
            for index, item in enumerate(COTIZACIONES)
            if index in selected_product_indexes
        ]
        selected_total = sum(
            int(
                "".join(
                    character
                    for character in item["precio"]
                    if character.isdigit()
                )
            )
            for item in selected_products
        )
        if selected_products:
            products_message = "\n".join(
                f"- {item['producto']}: {item['precio']}"
                for item in selected_products
            )
            products_message += (
                f"\nTotal de productos seleccionados: ₡"
                f"{selected_total:,}".replace(",", ".")
            )
        else:
            products_message = "No seleccioné productos."

        try:
            request_id = create_service_request(
                session[0] if session else None,
                name,
                customer_contact,
                message,
                selected_products,
                selected_total,
                company_variant,
            )
        except ServiceRequestLimitReached:
            self.send_html(
                429,
                get_home_page(
                    session[1] if session else None,
                    session[3] if session else "customer",
                    form_error=(
                        "La versión Normal alcanzó el límite mensual de 30 solicitudes. "
                        "Inténtalo el próximo mes o cambia a la versión Premium."
                    ),
                    csrf_token=csrf_token,
                    company_plan=company_plan,
                    company_variant=company_variant,
                ),
            )
            return


        # ====================================================
        # MOSTRAR EL MENSAJE EN LA TERMINAL
        # ====================================================

        print()

        print("=" * 60)

        print(
            "        NUEVO MENSAJE - ROCKET & VOLTIC"
        )

        print("=" * 60)

        print(
            f"Nombre del o de la cliente: {name}"
        )

        print()

        print(
            "Solicitud:"
        )

        print(
            message
        )

        print("=" * 60)

        print()


        # ====================================================
        # CREAR MENSAJE PARA WHATSAPP
        # ====================================================

        whatsapp_message = (
            "Hola Rocket & Voltic."
            "\n\n"
            f"Número de solicitud: #{request_id}"
            "\n\n"
            f"Mi nombre es: {name}"
            "\n"
            f"Contacto: {customer_contact}"
            "\n\n"
            "Y necesito ayuda con:"
            "\n"
            f"{message}"
            "\n\n"
            "Productos seleccionados:"
            "\n"
            f"{products_message}"
        )


        # ====================================================
        # CODIFICAR EL MENSAJE
        # ====================================================

        encoded_message = quote(
            whatsapp_message
        )


        # ====================================================
        # CREAR URL DE WHATSAPP
        # ====================================================

        whatsapp_url = (
            "https://wa.me/"
            + WHATSAPP_NUMBER
            + "?text="
            + encoded_message
        )
        tracking_html = (
            '<p>Puedes consultar el avance en <a href="/dashboard">Mi panel</a>.</p>'
            if company_plan == "pro" and session is not None else
            '<p>Guarda el número de solicitud para consultarlo al contactar con el taller.</p>'
        )


        # ====================================================
        # PÁGINA DE CONFIRMACIÓN
        # ====================================================

        confirmation_page = """

<!DOCTYPE html>

<html lang="es">

<head>

    <meta charset="UTF-8">

    <meta name="viewport"
          content="width=device-width, initial-scale=1.0">

    <title>
        Rocket & Voltic
    </title>


    <style>

        * {
            box-sizing: border-box;
            margin: 0;
            padding: 0;
        }


        body {

            min-height: 100vh;

            font-family:
                Arial,
                Helvetica,
                sans-serif;

            background:
                radial-gradient(
                    circle at top,
                    #06243d 0%,
                    #02070d 45%,
                    #000000 100%
                );

            color: white;

            display: flex;

            justify-content: center;

            align-items: center;

            padding: 30px;
        }


        .card {

            width: 100%;

            max-width: 600px;

            background:
                rgba(5, 12, 20, 0.96);

            border:
                1px solid #12344d;

            border-top:
                4px solid #0092fb;

            border-radius:
                14px;

            padding:
                40px;

            text-align:
                center;

            box-shadow:
                0 0 35px
                rgba(0, 146, 251, 0.15);
        }


        .check {

            font-size:
                55px;

            color:
                #25D366;

            margin-bottom:
                20px;
        }


        h1 {

            margin-bottom:
                15px;

            font-size:
                28px;
        }


        p {

            color:
                #b5c7d6;

            line-height:
                1.6;

            margin-bottom:
                20px;
        }


        .whatsapp {

            color:
                #25D366;

            font-weight:
                bold;
        }


        a {

            display:
                inline-block;

            padding:
                13px 25px;

            background:
                #0092fb;

            color:
                white;

            text-decoration:
                none;

            border-radius:
                8px;

            font-weight:
                bold;
        }


        a:hover {

            background:
                #007bd3;
        }

        .whatsapp-link {

            display: block;

            margin: 0 auto 18px;

            background: #25D366;

            color: #02070d;

            max-width: 260px;
        }

    </style>

</head>


<body>


    <div class="card">


        <div class="check">
            ✓
        </div>


        <h1>
            ¡Solicitud registrada!
        </h1>


        <p>
            Tu solicitud <strong>#__REQUEST_ID__</strong> quedó en espera de aceptación.
            Pulsa el botón para abrir WhatsApp y contactar con Rocket & Voltic.
        </p>

        __TRACKING_HTML__

        <a class="whatsapp-link"
           href="__WHATSAPP_URL__"
           target="_blank"
           rel="noopener noreferrer">
            Abrir WhatsApp
        </a>

        <p>

            Revisa el mensaje y pulsa

            <span class="whatsapp">
                Enviar
            </span>

            para contactar con
            Rocket & Voltic.

        </p>


        <a href="/contact">
            Volver al formulario
        </a>


    </div>


</body>

</html>

""".replace("__WHATSAPP_URL__", escape(whatsapp_url, quote=True)).replace(
    "__REQUEST_ID__", str(request_id)
).replace("__TRACKING_HTML__", tracking_html)


        # ====================================================
        # ENVIAR PÁGINA DE CONFIRMACIÓN
        # ====================================================

        encoded_page = confirmation_page.encode(
            "utf-8"
        )


        self.send_response(
            200
        )


        self.send_header(
            "Content-Type",
            "text/html; charset=utf-8"
        )


        self.send_header(
            "Content-Length",
            str(
                len(encoded_page)
            )
        )


        self.end_headers()


        self.wfile.write(
            encoded_page
        )

    def do_PUT(self):
        path = self.path.split("?", 1)[0]
        if path.startswith("/api/admin/requests/"):
            self.handle_admin_requests_request("PUT", path)
            return
        if path == "/api/records" or path.startswith("/api/records/"):
            self.handle_user_records_request("PUT", path)
            return
        self.send_error(404, "Página no encontrada")


    def do_DELETE(self):
        path = self.path.split("?", 1)[0]
        if path == "/api/records" or path.startswith("/api/records/"):
            self.handle_user_records_request("DELETE", path)
            return
        self.send_error(404, "Página no encontrada")


    def read_body(self, maximum_size):
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self.send_json(400, {"error": "Longitud de solicitud inválida."})
            return None

        if content_length <= 0 or content_length > maximum_size:
            self.send_json(413, {"error": "La solicitud está vacía o es demasiado grande."})
            return None

        return self.rfile.read(content_length)

    def read_json_object(self):
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            self.send_json(415, {"error": "El contenido debe enviarse como JSON."})
            return None

        body = self.read_body(12000)
        if body is None:
            return None
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self.send_json(400, {"error": "El cuerpo debe contener JSON válido."})
            return None
        if not isinstance(payload, dict):
            self.send_json(400, {"error": "El cuerpo debe ser un objeto JSON."})
            return None
        return payload


    def is_user_csrf_valid(self, session, submitted_token=None):
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
        except CookieError:
            return False
        csrf_cookie = cookie.get("rv_user_csrf")
        csrf_header = (
            submitted_token
            if submitted_token is not None
            else self.headers.get("X-CSRF-Token", "")
        )
        if csrf_cookie is None or not csrf_header:
            return False
        if not secrets.compare_digest(csrf_cookie.value, csrf_header):
            return False
        return secrets.compare_digest(
            hash_session_secret(csrf_header),
            session[2],
        )


    def handle_user_records_request(self, method, path):
        record_id = None
        is_collection = path == "/api/records"
        if not is_collection:
            record_path = path[len("/api/records/"):]
            if (
                not record_path
                or "/" in record_path
                or not record_path.isdecimal()
                or int(record_path) < 1
            ):
                self.send_json(404, {"error": "Registro no encontrado."})
                return
            record_id = int(record_path)

        session = self.get_user_session()
        if session is None:
            self.send_json(401, {"error": "Inicia sesión para acceder a tus registros."})
            return
        user_id = session[0]

        if method in ("POST", "PUT", "DELETE") and not self.is_user_csrf_valid(session):
            self.send_json(403, {"error": "La validación de seguridad de la solicitud falló."})
            return

        if method == "GET":
            if is_collection:
                self.send_json(200, {"records": list_user_records(user_id)})
            else:
                record = get_user_record(user_id, record_id)
                if record is None:
                    self.send_json(404, {"error": "Registro no encontrado."})
                else:
                    self.send_json(200, {"record": record})
            return

        if method == "POST":
            if not is_collection:
                self.send_json(404, {"error": "Registro no encontrado."})
                return
            payload = self.read_json_object()
            if payload is None:
                return
            title = payload.get("title")
            description = payload.get("description", "")
            if (
                not isinstance(title, str)
                or not title.strip()
                or len(title.strip()) > MAX_RECORD_TITLE_LENGTH
                or not isinstance(description, str)
                or len(description) > MAX_RECORD_DESCRIPTION_LENGTH
            ):
                self.send_json(
                    400,
                    {
                        "error": (
                            "El título es obligatorio (máximo 120 caracteres) y "
                            "la descripción debe ser texto de hasta 4000 caracteres."
                        )
                    },
                )
                return
            record = create_user_record(user_id, title.strip(), description.strip())
            self.send_json(201, {"record": record})
            return

        if method == "PUT":
            if is_collection:
                self.send_json(405, {"error": "Indica el ID del registro que quieres actualizar."})
                return
            payload = self.read_json_object()
            if payload is None:
                return
            title = payload.get("title")
            description = payload.get("description", "")
            if (
                not isinstance(title, str)
                or not title.strip()
                or len(title.strip()) > MAX_RECORD_TITLE_LENGTH
                or not isinstance(description, str)
                or len(description) > MAX_RECORD_DESCRIPTION_LENGTH
            ):
                self.send_json(
                    400,
                    {
                        "error": (
                            "El título es obligatorio (máximo 120 caracteres) y "
                            "la descripción debe ser texto de hasta 4000 caracteres."
                        )
                    },
                )
                return
            record = update_user_record(
                user_id,
                record_id,
                title.strip(),
                description.strip(),
            )
            if record is None:
                self.send_json(404, {"error": "Registro no encontrado."})
            else:
                self.send_json(200, {"record": record})
            return

        if method == "DELETE":
            if is_collection:
                self.send_json(405, {"error": "Indica el ID del registro que quieres eliminar."})
                return
            if not delete_user_record(user_id, record_id):
                self.send_json(404, {"error": "Registro no encontrado."})
            else:
                self.send_json(200, {"deleted": True})

    def handle_customer_requests_request(self):
        session = self.get_user_session()
        if session is None:
            self.send_json(401, {"error": "Inicia sesión para consultar tus solicitudes."})
            return
        company_variant = self.get_company_variant()
        if company_plan_for_variant(company_variant) != "pro":
            self.send_json(403, {"error": "El seguimiento en línea está disponible en la versión Premium."})
            return
        self.send_json(
            200,
            {
                "requests": list_customer_service_requests(
                    session[0],
                    company_variant,
                )
            },
        )


    def handle_admin_requests_request(self, method, path):
        session = self.get_user_session()
        if session is None:
            self.send_json(401, {"error": "Inicia sesión como administrador."})
            return
        if session[3] != "admin":
            self.send_json(403, {"error": "Solo el administrador puede gestionar solicitudes."})
            return
        company_variant = self.get_company_variant()
        company_plan = company_plan_for_variant(company_variant)

        if method == "GET":
            self.send_json(
                200,
                {
                    "requests": list_admin_service_requests(company_variant),
                    "plan": company_plan,
                    "company_variant": company_variant,
                    "monthly_count": current_month_request_count(
                        company_variant=company_variant
                    ),
                    "monthly_limit": (
                        FREE_MONTHLY_REQUEST_LIMIT
                        if company_plan == "free" else None
                    ),
                },
            )
            return

        request_path = path[len("/api/admin/requests/"):]
        if (
            not request_path
            or "/" in request_path
            or not request_path.isdecimal()
            or int(request_path) < 1
        ):
            self.send_json(404, {"error": "Solicitud no encontrada."})
            return
        if not self.is_user_csrf_valid(session):
            self.send_json(403, {"error": "La validación de seguridad de la solicitud falló."})
            return

        payload = self.read_json_object()
        if payload is None:
            return
        status = payload.get("status")
        solution = payload.get("solution", "")
        if (
            status not in SERVICE_REQUEST_STATUSES
            or not isinstance(solution, str)
            or len(solution) > 4000
        ):
            self.send_json(
                400,
                {"error": "El estado o la solución no tienen un formato válido."},
            )
            return
        try:
            updated = update_service_request(
                int(request_path),
                status,
                solution.strip(),
                company_variant,
            )
        except ValueError as error:
            self.send_json(409, {"error": str(error)})
            return
        if not updated:
            self.send_json(404, {"error": "Solicitud no encontrada."})
            return
        self.send_json(200, {"updated": True})


    def get_user_session(self):
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
        except CookieError:
            return None

        session_cookie = cookie.get("rv_user_session")
        if session_cookie is None:
            return None

        token_hash = hash_session_secret(session_cookie.value)
        now = int(time.time())
        with closing(connect_user_database()) as connection:
            session = connection.execute(
                """
                SELECT users.id, users.username, user_sessions.csrf_hash,
                       user_sessions.expires_at, users.role
                FROM user_sessions
                JOIN users ON users.id = user_sessions.user_id
                WHERE user_sessions.token_hash = ?
                """,
                (token_hash,),
            ).fetchone()
            if session is not None and session[3] <= now:
                with connection:
                    connection.execute(
                        "DELETE FROM user_sessions WHERE token_hash = ?",
                        (token_hash,),
                    )
                return None
        if session is None:
            return None
        return session[0], session[1], session[2], session[4]


    def get_user_session_username(self):
        session = self.get_user_session()
        return session[1] if session is not None else None


    def login_user(self):
        admin_login = parse_qs(self.path.partition("?")[2]).get("next") == ["admin"]
        body = self.read_body(4096)
        if body is None:
            return

        try:
            form_data = parse_qs(body.decode("utf-8"))
        except UnicodeDecodeError:
            self.send_html(
                400,
                get_account_page(
                    "login",
                    "El formulario no tiene un formato válido.",
                    admin_login=admin_login,
                ),
            )
            return

        username = form_data.get("username", [""])[0]
        password = form_data.get("password", [""])[0]
        authenticated_user = authenticate_user(username, password)
        if authenticated_user is None:
            self.send_html(
                401,
                get_account_page(
                    "login",
                    "Usuario o contraseña incorrectos.",
                    admin_login=admin_login,
                ),
            )
            return

        user_id, user_role = authenticated_user
        if admin_login and user_role != "admin":
            self.send_html(
                403,
                get_account_page(
                    "login",
                    "Esta cuenta no tiene permisos de administrador.",
                    admin_login=True,
                ),
            )
            return

        token = secrets.token_urlsafe(32)
        csrf_token = secrets.token_urlsafe(32)
        create_user_session(user_id, token, csrf_token)
        self.send_redirect(
            "/admin/requests" if admin_login else "/",
            cookies=(
                (
                    f"rv_user_session={token}; Path=/; HttpOnly; SameSite=Strict; "
                    f"Max-Age={USER_SESSION_MAX_AGE}{SECURE_COOKIE_ATTRIBUTE}"
                ),
                (
                    f"rv_user_csrf={csrf_token}; Path=/; SameSite=Strict; "
                    f"Max-Age={USER_SESSION_MAX_AGE}{SECURE_COOKIE_ATTRIBUTE}"
                ),
            ),
        )


    def register_user_account(self):
        body = self.read_body(4096)
        if body is None:
            return

        try:
            form_data = parse_qs(body.decode("utf-8"))
        except UnicodeDecodeError:
            self.send_html(400, get_account_page("register", "El formulario no tiene un formato válido."))
            return

        username = form_data.get("username", [""])[0]
        password = form_data.get("password", [""])[0]
        password_confirm = form_data.get("password_confirm", [""])[0]
        if password != password_confirm:
            self.send_html(
                400,
                get_account_page("register", "Las contraseñas no coinciden."),
            )
            return

        try:
            register_user(username, password)
        except ValueError as error:
            self.send_html(400, get_account_page("register", str(error)))
            return

        self.send_redirect("/account/login?created=1")


    def logout_user(self):
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
        except CookieError:
            cookie = SimpleCookie()
        session_cookie = cookie.get("rv_user_session")
        if session_cookie is not None:
            with closing(connect_user_database()) as connection:
                with connection:
                    connection.execute(
                        "DELETE FROM user_sessions WHERE token_hash = ?",
                        (hash_session_secret(session_cookie.value),),
                    )

        self.send_redirect(
            "/",
            cookies=(
                (
                    "rv_user_session=; Path=/; HttpOnly; SameSite=Strict; "
                    f"Max-Age=0{SECURE_COOKIE_ATTRIBUTE}"
                ),
                (
                    "rv_user_csrf=; Path=/; SameSite=Strict; "
                    f"Max-Age=0{SECURE_COOKIE_ATTRIBUTE}"
                ),
            ),
        )


    def send_html(self, status, page, cookie=None):
        encoded_page = page.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded_page)))
        self.send_header("Cache-Control", "no-store")
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(encoded_page)


    def send_json(self, status, payload):
        encoded_payload = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded_payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(encoded_payload)


    def send_redirect(self, location, cookie=None, cookies=()):
        self.send_response(303)
        self.send_header("Location", location)
        self.send_header("Cache-Control", "no-store")
        if cookie:
            self.send_header("Set-Cookie", cookie)
        for item in cookies:
            self.send_header("Set-Cookie", item)
        self.send_header("Content-Length", "0")
        self.end_headers()


    # ========================================================
    # MOSTRAR PÁGINA PRINCIPAL
    # ========================================================

    def show_landing(self):
        session = self.get_user_session()
        company_variant = self.get_company_variant()
        self.send_html(
            200,
            get_landing_page(
                session[1] if session else None,
                session[3] if session else "customer",
                company_variant,
            ),
        )


    def get_selected_company(self):
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
        except CookieError:
            return None

        selection = cookie.get("rv_company")
        if selection is None or selection.value not in COMPANY_VARIANTS:
            return None
        return selection.value

    def get_company_variant(self):
        return self.get_selected_company() or DEFAULT_COMPANY_VARIANT


    def select_company(self):
        body = self.read_body(4096)
        if body is None:
            return

        try:
            form_data = parse_qs(body.decode("utf-8"))
        except UnicodeDecodeError:
            self.send_html(
                400,
                get_company_selection_page("La selección no tiene un formato válido."),
            )
            return

        company_variant = form_data.get("variant", [""])[0]
        if company_variant not in COMPANY_VARIANTS:
            self.send_html(
                400,
                get_company_selection_page("Selecciona una versión válida."),
            )
            return

        self.select_company_variant(company_variant)

    def select_company_variant(self, company_variant, return_to="/"):
        if company_variant not in COMPANY_VARIANTS:
            self.send_error(404, "Versión de demostración no encontrada")
            return
        if return_to not in ("/", "/admin/requests"):
            self.send_error(400, "Destino de redirección no válido")
            return
        self.send_redirect(
            return_to,
            cookie=(
                f"rv_company={company_variant}; Path=/; "
                "HttpOnly; SameSite=Lax"
                f"{SECURE_COOKIE_ATTRIBUTE}"
            ),
        )


    def show_contact(self):
        session = self.get_user_session()
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
        except CookieError:
            cookie = SimpleCookie()
        csrf_cookie = cookie.get("rv_user_csrf")
        company_variant = self.get_company_variant()
        page = get_home_page(
            session[1] if session else None,
            session[3] if session else "customer",
            csrf_token=csrf_cookie.value if csrf_cookie else "",
            company_plan=company_plan_for_variant(company_variant),
            company_variant=company_variant,
        )


        encoded_page = page.encode(
            "utf-8"
        )


        self.send_response(
            200
        )


        self.send_header(
            "Content-Type",
            "text/html; charset=utf-8"
        )


        self.send_header(
            "Content-Length",
            str(
                len(encoded_page)
            )
        )


        self.end_headers()


        self.wfile.write(
            encoded_page
        )


# ============================================================
# INICIAR SERVIDOR
# ============================================================

def main():
    parser = argparse.ArgumentParser(description="Servidor Rocket & Voltic")
    parser.add_argument(
        "--add-admin-user",
        metavar="USUARIO",
        help="registra una cuenta administradora para gestionar solicitudes",
    )
    arguments = parser.parse_args()

    initialize_user_database()
    ensure_bootstrap_admin()

    if arguments.add_admin_user:
        password = getpass.getpass("Contraseña del administrador (mínimo 12 caracteres): ")
        confirmation = getpass.getpass("Confirma la contraseña: ")
        if password != confirmation:
            print("Error: las contraseñas no coinciden.")
            return 1
        try:
            register_user(arguments.add_admin_user, password, role="admin")
        except ValueError as error:
            print(f"Error: {error}")
            return 1
        print(f"Cuenta administradora creada para '{arguments.add_admin_user.strip()}'.")
        return 0

    server = ThreadingHTTPServer(
        (HOST, PORT),
        RocketAndVoltic
    )


    print()

    print("=" * 60)

    print(
        "             ROCKET & VOLTIC"
    )

    print("=" * 60)

    print()

    print(
        "Servidor iniciado correctamente."
    )

    print()

    database_label = (
        "PostgreSQL (DATABASE_URL)" if DATABASE_URL else USER_DATABASE_PATH
    )
    print(f"Base de datos de cuentas y solicitudes: {database_label}")
    print(
        "Versiones disponibles: Normal (30 solicitudes al mes) y Premium (ilimitado)"
    )

    print(
        f"Para crear un administrador: python \"{os.path.basename(__file__)}\" --add-admin-user USUARIO"
    )

    print()

    public_host = "localhost" if HOST in ("0.0.0.0", "::") else HOST
    print(
        f"Página: http://{public_host}:{PORT}"
    )
    print(
        f"Demo normal: http://{public_host}:{PORT}/demo/normal"
    )
    print(
        f"Demo premium: http://{public_host}:{PORT}/demo/premium"
    )

    print()

    print(
        f"Estado: http://{public_host}:{PORT}/healthz"
    )

    print()

    print(
        "WhatsApp:"
    )

    print(
        "+506 8634 1818"
    )

    print()



    print()

    print(
        "Presiona CTRL + C para detener el servidor."
    )

    print()

    print("=" * 60)

    print()


    try:
        server.serve_forever()

    except KeyboardInterrupt:

        print()

        print(
            "Servidor detenido."
        )

        server.server_close()

    except OSError as error:
        server.server_close()
        raise RuntimeError(f"No se pudo mantener el servidor activo: {error}") from error

    return 0


if __name__ == "__main__":
    raise SystemExit(main())