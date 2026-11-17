"""Acceso a SQLite para el pipeline de empleo (Fase 1).

La tabla ``vacantes`` se define en ``schema.sql``. ``empresa`` y ``url`` se
guardan como cadena vacía (nunca NULL) para que ``UNIQUE(empresa, titulo, url)``
deduplique bien: en SQLite dos NULL nunca son iguales entre sí.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
SCHEMA_PATH = BASE_DIR / "schema.sql"
DEFAULT_DB = "vacantes.db"
MAX_DESCRIPCION = 8000

COLUMNAS_INSERT = (
    "titulo", "empresa", "ubicacion", "modalidad", "sueldo", "sueldo_min_mxn",
    "sueldo_max_mxn", "url", "fecha_publicacion", "fuente", "aristas",
    "keywords_matched", "descripcion", "fecha_scrapeo",
)


def connect(db_path: str | Path = DEFAULT_DB) -> sqlite3.Connection:
    """Abre la base de datos con filas accesibles por nombre de columna."""
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(db_path: str | Path = DEFAULT_DB) -> sqlite3.Connection:
    """Crea la tabla ``vacantes`` y sus índices si no existen; devuelve la conexión."""
    conn = connect(db_path)
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    conn.commit()
    return conn


def insert_vacante(conn: sqlite3.Connection, vacante: dict) -> bool:
    """Inserta una vacante con INSERT OR IGNORE.

    Devuelve True si la fila es nueva y False si ya existía (duplicado por
    empresa + título + URL). Normaliza empresa/url a '' y trunca la descripción.
    """
    fila = dict(vacante)
    fila["empresa"] = (fila.get("empresa") or "").strip()
    fila["url"] = (fila.get("url") or "").strip()
    if fila.get("descripcion"):
        fila["descripcion"] = fila["descripcion"][:MAX_DESCRIPCION]
    marcadores = ", ".join("?" for _ in COLUMNAS_INSERT)
    cur = conn.execute(
        f"INSERT OR IGNORE INTO vacantes ({', '.join(COLUMNAS_INSERT)}) "
        f"VALUES ({marcadores})",
        [fila.get(col) for col in COLUMNAS_INSERT],
    )
    return cur.rowcount == 1
