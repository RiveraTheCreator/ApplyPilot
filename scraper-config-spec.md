# Spec de configuración del scraper — Mi Primera Chamba (compartido por Alex, 2026-11-15)

Origen: mensaje de Alejandro con el prompt del "Ingeniero de Datos y Arquitecto de Scraping" de Mi Primera Chamba. Es la especificación para compilar una Estrategia de Empleabilidad (Notion) en perfiles de búsqueda JobSpy (JSON).

## Resumen operativo

- Transformar la estrategia en **15–25 perfiles de búsqueda** (objetos JSON) listos para un scraper multiplataforma (JobSpy).
- Cada perfil: `name`, `site_name`, `search_term`, `location`, `results_wanted` (mínimo); opcionales: `country_indeed`, `is_remote`, `easy_apply`, `job_type`, `hours_old`.
- `name` único, sin espacios, formato `{Numero}_{Frente}_{MicroNicho}_{Plataforma}` (ej. `1_F1_TechnicalSupportSr_indeed`).
- Frentes: **F1** Capitalización Rápida, **F2** Red de Seguridad, **F3** Infiltración Estratégica, **F4** Zona de Genio.
- Distribución por plataforma: Indeed 40%, LinkedIn 30%, Glassdoor 20%, ZipRecruiter 10%. **Si la estrategia apunta a LATAM o Europa, NO generar ZipRecruiter** (redistribuir entre Indeed, LinkedIn y Glassdoor).

## Reglas por plataforma

- **Indeed**: sí booleanos (`OR`, `AND`, `-`). Siempre `country_indeed` (país en inglés). No usar `"Remote"` en `location` (ciudad o país real). Si remoto: `"is_remote": true`. Si `is_remote` o existe `job_type`, prohibido `hours_old`.
- **LinkedIn**: no booleanos; keywords simples separadas por espacios. `results_wanted` ≤ 100. `location` flexible (`"Worldwide"`, `"Remote"`, `"LATAM"`, `"Mexico"`). Si `easy_apply: true`, prohibido `hours_old`.
- **Glassdoor**: no booleanos. Siempre `country_indeed`. `location` estricto ciudad y estado o país (`"Austin, TX"`, `"Guadalajara, Mexico"`).
- **ZipRecruiter**: no booleanos. Solo Estados Unidos o Canadá. `location` formato `"City, State"`. No generar para LATAM/Europa.

## Reglas críticas de JSON

- Array JSON válido, pretty-printed, indentación de 2 espacios, empieza con `[` y termina con `]`. Sin comentarios ni trailing commas. `site_name` en minúsculas, solo: `indeed`, `linkedin`, `glassdoor`, `zip_recruiter`.

## Aplicación a este pipeline (Alex, 2026-11-15)

- Estrategia: 100% remoto, contratación desde México, LATAM → **sin ZipRecruiter**.
- Perfiles generados en `search-profiles.json` (20 perfiles: Indeed 8, LinkedIn 7, Glassdoor 5) desde `keywords.json` (11 aristas).
- El scraper (`scraper.py`) debe consumir `search-profiles.json` como fuente de perfiles JobSpy.
- Estado real 2026-11-15 19:06: JobSpy-LinkedIn e Indeed funcionan; Glassdoor falla (curl 56) y ZipRecruiter 403 (pide proxy) en la corrida de verificación — por eso la distribución real se apoya en Indeed/LinkedIn y Firecrawl cubre career pages.

## Ampliación regional (Ranker v2, 2026-11-16)

- El build prompt del Ranker v2 (orden de Alex 2026-11-15 21:29) amplía la cobertura a
  LATAM + América del Norte: MX, CO, AR, BR, CL, PE, EC, UY, PY, BO, GT, CR, PA, DO, US, CA.
  Es posterior a este spec y **lo reemplaza en dos puntos**: (1) el tope de 15–25 perfiles
  (ahora 59: 20 base + 39 regionales) y (2) "sin ZipRecruiter": se generan perfiles de
  ZipRecruiter **solo para US y CA**, como dice la regla por plataforma.
- Perfiles regionales generados por `build_regional_profiles.py` (añade, no regenera;
  países desde `filter-config.json › countries`). Siempre con filtro remoto. Indeed solo
  en países con dominio Indeed en JobSpy (PY, BO, GT, DO no tienen → solo LinkedIn).
  Glassdoor solo AR, BR, US, CA (+ MX). Distribución: Indeed 19, LinkedIn 29, Glassdoor 9,
  ZipRecruiter 2.
- Estado real: Indeed / Glassdoor / ZipRecruiter siguen dando 403 de forma intermitente o
  persistente; los perfiles son válidos pero hoy LinkedIn es la fuente que más trae. El
  health JSON (`logs/filter-health-*.json`) lo marca como alerta `source_zero`.
