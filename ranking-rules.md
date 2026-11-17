# Reglas de ranking v2 — actualizadas por Alex, 2026-11-15 21:29

## Principio
El ranker es interno y por **estrellas (1–5)**. Casi nada se descarta: el objetivo es
ordenar, no podar. Solo se descarta lo que es 100% presencial y no tiene ningún
componente remoto.

## Único filtro duro
- **100% presencial (on-site, sin remoto ni híbrido): fuera.** Todo lo demás
  (híbrido, remoto con letra chica, visa, relocación, sueldo bajo o desconocido)
  se queda en el pool y baja estrellas, nunca se descarta.
- NO existe filtro por años de experiencia ni por título "senior": los años
  pedidos son la señal más débil del score (0 a 5 puntos, nunca penaliza).

## Estrellas (internas, visibles en el CRM)
- **5★**: 100% remoto + encaje fuerte de keywords + (sueldo en/encima del objetivo
  o proceso rápido/reciente) — prioridad máxima para Fase 2 (CV) y Fase 3 (apply).
- **4★**: 100% remoto + buen encaje; sueldo desconocido o ligeramente abajo.
- **3★**: híbrido con buen encaje, o remoto con encaje débil / sueldo bajo.
- **2★**: híbrido con encaje débil, o remoto con fricción (visa/relocación,
  sueldo claramente abajo).
- **1★**: se conserva pero con prioridad mínima (p. ej. híbrido + sueldo bajo
  + encaje débil). Sirve para exploración ocasional, no para el flujo principal.

## Sueldo (se conserva, pero con menos peso)
- Muchas vacantes no publican rango salarial, así que el sueldo NO es lo más
  ponderante: su peso máximo baja (de 15 a 8 puntos). Desconocido = neutro
  (4 puntos), nunca cero y nunca inventado.
- El sueldo solo baja estrellas; nunca descarta por sí solo.

## Cobertura regional
- La búsqueda es regional: combinaciones por país en toda LATAM y América del
  Norte (MX, CO, AR, BR, CL, PE, EC, UY, PY, BO, GT, CR, PA, DO, US, CA),
  siempre con filtro remoto. Respetar las reglas por plataforma de
  `scraper-config-spec.md` (Indeed: `country_indeed`; LinkedIn: sin booleanos;
  ZipRecruiter solo US/CA).
- Lo que importa es llegar a las entrevistas correctas: alineación del rol con
  las 11 aristas, 100% remoto, sueldo $35,000–$40,000 MXN, proceso rápido
  (Easy Apply, contratación inmediata, publicación < 7 días), boost sector
  hotelería (arista 6).

## Autovigilancia del filtro (mecanismo de detección y corrección)
- Cada corrida registra salud del filtro en `logs/filter-health-YYYY-MM-DD.json`:
  total evaluadas, tasa de pase, desglose de motivos de descarte, tasa de pase
  por fuente, rendimiento por keyword (vacantes por keyword) y por país.
- Alertas (se reportan en el review diario): tasa de pase fuera de rango
  (demasiado estricto o demasiado laxo), una fuente entera en 0, una keyword
  con 0 resultados N días seguidos, un motivo de descarte que se dispara, o
  una caída brusca de vacantes nuevas vs el promedio.
- Corrección: los pesos, umbrales y el on/off de keywords viven en un archivo
  de configuración versionado (`filter-config.json`) con changelog (qué cambió,
  por qué, cuándo, quién). Kavi y los agentes pueden corregir la configuración
  cuando una alerta lo justifique; cada cambio se reporta en el review diario
  para que Alex lo vea y lo pueda vetar. Nada de cambios silenciosos.
