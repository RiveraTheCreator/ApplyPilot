-- Esquema SQLite del pipeline de empleo (Fase 1).
-- empresa y url se guardan como '' (no NULL) cuando faltan, para que
-- UNIQUE(empresa, titulo, url) deduplique correctamente (en SQLite, NULL != NULL).

CREATE TABLE IF NOT EXISTS vacantes (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    titulo            TEXT NOT NULL,
    empresa           TEXT NOT NULL DEFAULT '',
    ubicacion         TEXT,
    modalidad         TEXT,              -- remoto | hibrido | presencial | no especificada
    sueldo            TEXT,              -- texto crudo tal como vino de la fuente
    sueldo_min_mxn    REAL,              -- parseado, MXN/mes (NULL = desconocido)
    sueldo_max_mxn    REAL,              -- parseado, MXN/mes (NULL = desconocido)
    url               TEXT NOT NULL DEFAULT '',
    fecha_publicacion TEXT,              -- YYYY-MM-DD
    fuente            TEXT,              -- linkedin | indeed | glassdoor | zip_recruiter
    aristas           TEXT,              -- JSON: lista de ids de arista
    keywords_matched  TEXT,              -- JSON: lista de keywords encontradas
    descripcion       TEXT,              -- truncada a 8000 caracteres
    estado            TEXT DEFAULT 'nueva',
    score             REAL,
    pasa_filtro       INTEGER DEFAULT NULL,  -- NULL = sin evaluar, 1 = pasa, 0 = descartada
    estrellas         INTEGER,           -- 1-5 (Ranker v2); NULL = descartada (100% presencial) o sin evaluar
    modalidad_v2      TEXT,              -- remoto | hibrido | no_especificada | presencial (detector v2)
    fecha_scrapeo     TEXT,              -- ISO 8601 UTC
    UNIQUE (empresa, titulo, url)
);

CREATE INDEX IF NOT EXISTS idx_vacantes_estado        ON vacantes (estado);
CREATE INDEX IF NOT EXISTS idx_vacantes_fuente        ON vacantes (fuente);
CREATE INDEX IF NOT EXISTS idx_vacantes_fecha_scrapeo ON vacantes (fecha_scrapeo);

-- Fase 2 (cvgen.py): registro de CVs por vacante. Tabla aparte para no tocar columnas del ranker.
CREATE TABLE IF NOT EXISTS cv_generaciones (
    vacante_id     INTEGER PRIMARY KEY REFERENCES vacantes(id),
    estado         TEXT NOT NULL,      -- generado | duplicado | pendiente_descripcion | error
    cv_file        TEXT,               -- nombre del .docx en ~/workspace/cv/
    cv_version     TEXT,               -- "v1 2026-11-16"
    version_n      INTEGER,
    idioma         TEXT,               -- es | en
    familia        TEXT,               -- familia de rol (ops, customer, solutions, ai_eng, network, pm)
    duplicado_de   INTEGER,            -- vacante representante si es la misma (empresa, título)
    motivo         TEXT,
    cobertura      TEXT,               -- JSON del reporte de cobertura
    actualizado    TEXT                -- ISO 8601 UTC
);
