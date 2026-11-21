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

## 2.1.0 — 2026-11-20 22:30 CST — coordinador (propuesta subagente reapunte 2026-11-20; cableado por sesión Opus auditoría 2026-11-20)
- Qué: nueva penalización por título senior y por región de contratación excluida (caps de estrellas, NO filtros duros).
  - `seniority.title_re` (solo título: senior|chief|principal|staff|director|head of|vp|vice president|sr.|lead — excepto "lead generation") → `seniority_title_cap` 3, `score_penalty` 10. "Manager" a secas NO penaliza (arista 11).
  - `region.*` (título + ubicación; objetivo US/CA/MX/LATAM) → `region_excluded_cap` 2, `score_penalty` 10; región desconocida neutra; `london` excluido de la lista (London, Ontario).
  - `weights.exp_*` SIN CAMBIOS: los años pedidos siguen siendo la señal más débil y nunca filtran (regla de Alex).
  - `hard_filter` sin cambios: el único descarte sigue siendo 100% presencial.
  - Cableado en `filter_rank.py`: `compute_stars()` aplica los caps tras todas las demás reglas; `score_row()` resta las penalizaciones y añade los flags `senior_title` / `region_excluded`.
- Por qué: reapunte 2026-11-20 (`docs/reapunte-2026-11-20.md`). 0 entrevistas en 31 enviadas; 50 de 205 4★+ en `nueva` son títulos senior (incl. 3× Viva Sr RevOps, ya rechazado) y 10 son EU/AS-only (Pearson Polonia aplicada como 4★; Smart Working India en 5★, ya descartada por Alex). Umbrales 15/25 cruzados (monitor 2026-11-20: apply CRÍTICO, embudo CRÍTICO).
- Efecto medido (copia de vacantes.db, re-rank fresco config 2.0.0 como base): pasan 2.318/2.429 (95,4%, sin cambio); descartadas 111 (sin cambio). Estrellas: 5★ 19→7 · 4★ 218→160 · 3★ 589→631 · 2★ 728→756 · 1★ 764. 4★+ total 237→167 (−70); 4★+ en `nueva` 205→146; 5★ en `nueva` 8→3; 4★+ en `nueva` sin CV 116→87. 595 filas tocadas (estrellas y/o score). 14 entradas de cola afectadas (6 pendientes, 8 bloqueadas).
