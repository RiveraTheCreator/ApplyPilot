# Changelog del filtro / ranker (`filter-config.json`)

Cada cambio en `filter-config.json`, en el detector presencial de `filter_rank.py` o
en los perfiles de búsqueda que afecte el ranking se anota aquí: **qué** cambió, **por
qué** (qué alerta o qué orden lo justificó), **cuándo** y **quién**. Además se
sube el `version` de `filter-config.json` y se reporta en el review diario para que
Alex lo vea y lo pueda vetar. Nada de cambios silenciosos.

Formato:

```
## <version> — YYYY-MM-DD HH:MM CST — <quién>
- Qué: <campo> <valor anterior> → <valor nuevo>
- Por qué: <alerta de logs/filter-health-YYYY-MM-DD.json u orden de Alex>
- Efecto medido: <conteo por estrella / tasa de pase antes → después>
```

---

## 2.0.0 — 2026-11-16 ~01:00 CST — claude-code (build Ranker v2, ordenado por Alex 2026-11-15 21:29)
- Qué: ranker v1 (3 filtros duros + score) → ranker v2 (estrellas 1–5, único descarte = 100% presencial).
  - Descartes v1 eliminados: híbrido, visa/relocación, sueldo < $35k. Ahora bajan estrellas.
  - `weights.salary_target` 15 → 8; `salary_unknown` 7 → 4 (neutro, nunca 0, nunca inventado).
  - Nuevos: `stars.*` (umbrales), `keywords.disabled` (vacío), `countries` (16 países),
    `health.*` (umbrales de alertas).
  - Detector presencial nuevo (`classify_modality`): negaciones ("no presencial",
    "no requiere asistir a la oficina", "100% on-site: no"), menciones neutras
    (entrevistas/eventos/offsites/visitas ocasionales en persona), jerga técnica
    ("soporte remoto", "asistencia remota"), porcentajes híbridos ("70% presencialidad").
    La etiqueta de Indeed "Tipo/Lugar de trabajo: empleo presencial" es señal normal,
    no fuerte (aparece también en vacantes remotas).
- Por qué: `ranking-rules.md` v2 — ordenar, no podar.
- Efecto medido (767 filas): v1 pasaban 142 / 767 (18.5%) → v2 pasan 693 / 767 (90.3%);
  descartes 74, todos por 100% presencial. Estrellas: 5★ 8 · 4★ 24 · 3★ 165 · 2★ 170 · 1★ 326.
  De las 142 que pasaban en v1: 1 ahora se descarta (id 106, "100% onsite" explícito, que v1
  dejaba pasar por error).
- Perfiles: `search-profiles.json` 20 → 59 (+39 regionales LATAM + US/CA, vía
  `build_regional_profiles.py`).
