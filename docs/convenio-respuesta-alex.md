# Convención RESPUESTA-ALEX — respuestas de Alex desde la pestaña Pendientes del CRM

Fecha: 2026-11-20. Decisión de Alex (22:01): en el CRM (artefacto `crm-empleo`) hay una
pestaña "Pendientes" con tarjetas por vacante (empresa, título, pregunta EXACTA) y
opciones clicables + campo de texto. Al contestar, la pestaña escribe la respuesta
con la acción `updateVacancy` del artefacto, con el marcador siguiente.

## El marcador (lo que escribe la pestaña)

- `notes`: se AGREGA (nunca se borra lo anterior) una línea:
  `RESPUESTA-ALEX [YYYY-MM-DD HH:MM CST]: <pregunta exacta> → <respuesta>`
- `nextStep`: se escribe `RESPUESTA-ALEX: <respuesta corta> — lista para continuar`
- `status`: se deja como está (normalmente `nueva`).

## Quién lo lee y qué hace

- El agente principal y el cron diario `empleo-aplicar-diario` (07:00) leen las filas del
  CRM cuyo `nextStep` empiece por `RESPUESTA-ALEX:`.
- Por cada fila marcada: se busca la entrada en `apply-queue.json` (mismo `vacante_id`),
  se pasa a `en_curso` (edición manual con nota en `historial` — el módulo no tiene
  transición PREGUNTA→en_curso), se continúa la aplicación en el navegador con la
  respuesta, y se cierra con `autoapply.py --report` + sync al CRM con `updateVacancy`.
- La respuesta TAMBIÉN se registra en `apply-config.json › respuestas_conocidas`
  (LISTA de {id, pregunta_tipo, es, en, fuente}) cuando sea reutilizable, con fuente
  `Alex, CRM Pendientes <fecha>`.
- Las tarjetas ya contestadas (`nextStep` con prefijo `RESPUESTA-ALEX:`) se muestran
  como resueltas en la pestaña, con la respuesta visible y un botón "cambiar respuesta"
  (que sobrescribe el marcador con la nueva respuesta y fecha).

## Reglas

- La pregunta en la tarjeta es la EXACTA del `nextStep` ("Pregunta para Alex: ...");
  nunca se reescribe ni se resume.
- Nunca se inventan preguntas ni respuestas en la pestaña.
- El cron de las 07:00 NO lee respuestas de ningún otro sitio: solo el marcador
  `RESPUESTA-ALEX:` en el CRM.
