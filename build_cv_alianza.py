# -*- coding: utf-8 -*-
"""Genera el CV a medida para Alianza Corp (Arquitecto de Soluciones Microsoft 365 e IA).
Reglas: prompt de Alex (cv-prompt.md) + skill ATS. Sin mentiras, sin emojis, sin tablas,
una sola columna, ATOP, etiquetas e-mail:/LinkedIn:, Core Skills al final. En español.
"""
from docx import Document
from docx.shared import Pt, Cm
from docx.enum.text import WD_ALIGN_PARAGRAPH

OUT = "/home/hatch/workspace/cv/cv-alianza-corp-arquitecto-soluciones-m365-ia.docx"

doc = Document()

# Estilo base: una columna, alineado a la izquierda
style = doc.styles["Normal"]
style.font.name = "Calibri"
style.font.size = Pt(11)
style.paragraph_format.space_after = Pt(4)
style.paragraph_format.space_before = Pt(0)
for section in doc.sections:
    section.top_margin = Cm(1.8)
    section.bottom_margin = Cm(1.8)
    section.left_margin = Cm(2.0)
    section.right_margin = Cm(2.0)

def heading(text):
    p = doc.add_paragraph()
    p.paragraph_format.space_before = Pt(12)
    p.paragraph_format.space_after = Pt(4)
    r = p.add_run(text.upper())
    r.bold = True
    r.font.size = Pt(12)
    return p

def body(text):
    p = doc.add_paragraph(text)
    p.paragraph_format.space_after = Pt(4)
    return p

def bullet(text):
    p = doc.add_paragraph(style="List Bullet")
    p.paragraph_format.space_after = Pt(2)
    p.paragraph_format.left_indent = Cm(0.6)
    p.add_run(text)
    return p

def job(title, org, dates, location):
    p = doc.add_paragraph()
    p.paragraph_format.space_before = Pt(8)
    p.paragraph_format.space_after = Pt(2)
    r = p.add_run(f"{title} | {org}")
    r.bold = True
    r.font.size = Pt(11)
    p2 = doc.add_paragraph(f"{dates} | {location}")
    p2.paragraph_format.space_after = Pt(2)
    return p

# --- Encabezado ---
p = doc.add_paragraph()
p.alignment = WD_ALIGN_PARAGRAPH.LEFT
p.paragraph_format.space_after = Pt(2)
r = p.add_run("Alejandro Rivera Barrón")
r.bold = True
r.font.size = Pt(20)

p = doc.add_paragraph()
p.paragraph_format.space_after = Pt(4)
r = p.add_run("Arquitecto de Soluciones de IA  ·  Del readiness assessment al blueprint de despliegue")
r.font.size = Pt(12)

p = doc.add_paragraph()
p.paragraph_format.space_after = Pt(2)
r = p.add_run("e-mail: isaac.alejandrorb@gmail.com  |  LinkedIn: linkedin.com/in/alejandro-rivera-ai  |  github.com/RiveraTheCreator  |  +52 56 3702 5292  |  Ciudad de México, México")
r.font.size = Pt(10)

# --- Perfil ---
heading("Perfil")
body("Ingeniero en Sistemas Computacionales (ESCOM-IPN, titulado) que diseña arquitecturas de soluciones "
     "para negocios con operación desordenada, con un método fijo: evaluación de readiness en sitio, "
     "blueprint de despliegue, prueba de concepto (POC) y producción. Combina diagnóstico con el cliente, "
     "diseño de controles de seguridad y gobierno, documentación técnica y construcción de agentes de "
     "inteligencia artificial (IA) con n8n e interfaces de programación de aplicaciones (API), y lleva las "
     "soluciones a producción solo cuando mueven un indicador. Trabaja con gerencia y equipos operativos "
     "para que la solución se adopte, no solo se entregue. Cuenta con Microsoft Azure Fundamentals (AZ-900) "
     "y ejerce como embajador oficial de Base44.")

# --- Experiencia ---
heading("Experiencia")

job("Fundador y Consultor de Soluciones de IA", "EngemIA",
    "Ene 2026 - actualidad", "Ciudad de México, México")
bullet("Diseña e implementa sistemas con inteligencia artificial (IA) para negocios con operación "
       "desordenada, sobre todo hotelería independiente, con un método fijo: evaluación de readiness en "
       "sitio, blueprint de automatización, prueba de concepto (POC) y producción.")
bullet("Estandariza la entrada de clientes con un blueprint pagado que documenta mapa de procesos, "
       "priorización de automatizaciones y estimación de implementación antes de construir.")
bullet("Empaqueta como producto reutilizable el sistema del Hotel del Principado (check-in digital, base "
       "propia de huéspedes, solicitud de reseñas y campañas de WhatsApp) bajo la marca Kit Hotel para "
       "llevarlo a otros hoteles independientes.")
bullet("Construye un sistema de reactivación de prospectos Business-to-Business (B2B) con n8n, WhatsApp "
       "Cloud API, OpenAI y Chatwoot que genera 302 envíos, 118 respuestas (39 %) y 35 prospectos calificados.")

job("Arquitecto Tecnológico Externo", "Hotel del Principado",
    "Ago 2025 - actualidad", "Ciudad de México, México")
bullet("Ejerce como arquitecto tecnológico externo con iguala mensual: diseña y opera el backend del hotel "
       "y define el plan tecnológico con gerencia y dirección.")
bullet("Pone en producción un check-in digital que registra a cada huésped en ~30 segundos y crea la "
       "primera base de datos propia de huéspedes del hotel.")
bullet("Opera campañas por la API oficial de WhatsApp que generan 126 mensajes y 10 reservas pagadas "
       "(~$9,500 MXN), con prospectos a ~$7 MXN frente a $143-171 vía agencia; el sistema genera los "
       "prospectos y el equipo del hotel cierra.")
bullet("Ejecuta una auditoría de seguridad que detecta 4 hallazgos críticos (Domain-based Message "
       "Authentication, Reporting and Conformance, DMARC, panel administrativo expuesto, subdominio "
       "anómalo) y diseña la arquitectura de integración entre motor de reservas y Property Management "
       "System (PMS), con n8n como capa de conciliación.")
bullet("Define el plan del cuarto trimestre de 2026 con gerencia: triaje de reseñas, landing de reserva "
       "directa y tablero de datos.")

job("Recepcionista - Consultor de TI", "Hotel del Principado",
    "Dic 2024 - Ago 2025", "Ciudad de México, México")
bullet("Detectó desde recepción fallas de red y de experiencia del huésped y las validó con una encuesta a "
       "105 huéspedes antes de proponer soluciones.")
bullet("Diseñó e instaló la red de planta baja a cuarto piso: cableado troncal, puntos Ethernet y nodos "
       "mesh TP-Link.")
bullet("Vendió y cerró el primer contrato como consultor: propuesta, cronograma, presupuesto, contrato y "
       "facturación.")

job("Cofundador", "Mi Primera Chamba",
    "Ene 2026 - Nov 2026", "Ciudad de México, México")
bullet("Cofundó un producto de búsqueda de empleo con inteligencia artificial (IA) que genera búsqueda de "
       "vacantes y currículum a la medida, con +35 entrevistas y 5 clientes pagados.")

job("Investigador", "Laboratorio de Ciberseguridad, CIC-IPN",
    "Ene 2023 - Dic 2024", "Ciudad de México, México")
bullet("Construyó Tlaotic, urna electrónica sobre Hyperledger Fabric con Go y Docker, prototipo Technology "
       "Readiness Level (TRL) 3 validado con 10 series de pruebas de carga de 3 a 12 nodos.")
bullet("Coautoró el artículo publicado en IEEE ARGENCON 2024 sobre Tlaotic (ver Publicación).")

# --- Proyectos y comunidad ---
heading("Proyectos y comunidad")
bullet("Organizó Anti Tech Week, Mexico Tech Week 2026 (octubre de 2026): programa de 5 días con "
       "hackatón, 250+ registros y 30+ en lista de espera; construyó en Base44 la plataforma interna para "
       "operarlo.")
bullet("Ejerce como embajador oficial de Base44 (septiembre de 2026 - actualidad): imparte Ship It Live, "
       "taller de 90 minutos en el que personas sin experiencia técnica publican su primera aplicación.")
bullet("Llegó al Top 10 de equipos en el Incubathon de hospitalidad (julio de 2026), con premio de equipo "
       "de US$1,000 y Top 3 para beca.")
bullet("Automatizó envíos personalizados con n8n, SendGrid y JavaScript desde Excel y redujo el proceso "
       "por cierre de 3-4 horas a 2 minutos.")

# --- Publicación ---
heading("Publicación")
body("Rivera Barrón, I. A., Escalona, D. F., Anaya, E. A. y García, G. G. (2024). Tlaotic: Blockchain-Based "
     "Digital Ballot Box Compatible with Mexican Electoral Processes. 2024 IEEE Biennial Congress of "
     "Argentina (ARGENCON), pp. 1-8. doi:10.1109/ARGENCON62399.2024.10735899")

# --- Educación ---
heading("Educación")
bullet("2019 - 2024: Ingeniería en Sistemas Computacionales, Escuela Superior de Cómputo (ESCOM), "
       "Instituto Politécnico Nacional. Titulado.")
bullet("2023: Cisco Networking Academy: redes de computadoras y telecomunicaciones.")
bullet("2021: Bootcamp de Desarrollo Web Full Stack, Digital House (Node.js, Express, React, MySQL).")

# --- Habilidades y certificaciones ---
heading("Habilidades y certificaciones")
bullet("Consultoría: discovery en sitio, encuestas y entrevistas, mapeo de procesos, PRD y especificación, "
       "roadmap, propuestas, cronogramas, presupuestos y contratos, capacitación de equipos.")
bullet("IA y automatización: agentes de IA, n8n, OpenAI, Claude, Gemini, Claude Code, Base44, WhatsApp "
       "Business API, Chatwoot.")
bullet("Desarrollo: Python, JavaScript/Node.js, Next.js, React, SQL, Go, Docker, APIs REST y webhooks.")
bullet("Infraestructura: diseño y diagnóstico de redes, auditoría de seguridad web y correo. Microsoft "
       "Azure Fundamentals (AZ-900), Huawei Cloud Developer (HCCDA).")
bullet("Idiomas: español nativo. Inglés B2+ (certificado iTEP).")

# --- Core Skills ---
heading("Core Skills")
body("Arquitectura de soluciones, evaluación de readiness, blueprint de despliegue, Microsoft Azure "
     "Fundamentals (AZ-900), diseño de controles de seguridad, gobierno de soluciones, documentación "
     "técnica, agentes de inteligencia artificial (IA), n8n, automatización de flujos de trabajo, "
     "integración de APIs, WhatsApp Business API, Chatwoot, Python, JavaScript, SQL, Docker, auditoría de "
     "seguridad, mapeo de procesos, consultoría, preventa, diseño de soluciones cloud, gestión de "
     "proyectos, capacitación de equipos, inglés B2+")

doc.save(OUT)
print("OK:", OUT)
