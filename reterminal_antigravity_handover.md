# PROYECTO RETERMINAL E1002 (SPECTRA 6) — HANDOVER & ESPECIFICACIÓN TÉCNICA
Documento maestro para la migración del proyecto hacia Antigravity. Resume el propósito, hardware, arquitectura, lógica de negocio de cada módulo, evolución histórica, problemas resueltos y hoja de ruta.

## 1. Visión General del Proyecto
* **Objetivo principal:** Servidor generador de un dashboard ejecutivo e-paper de actualización periódica para un dispositivo Seeed Studio reTerminal E1002, montado en el escritorio de trabajo de Pablo Alvarez.
* **Paradigma de funcionamiento:**
  * El dispositivo cliente (reTerminal E1002 con pantalla E-Ink Spectra 6) actúa como visualizador pasivo. Realiza consultas HTTP periódicas (cada 5 a 10 minutos) mediante curl/wget a un endpoint `/dashboard.png`.
  * El servidor corre en segundo plano (`background_worker_loop`), recopila información de múltiples fuentes (calendario corporativo, finanzas en tiempo real, meteorología oficial y tráfico en ruta laboral), renderiza la imagen completa en memoria RAM en formato PNG estricto de 800×480 píxeles y la sirve de forma atómica en menos de 5 ms.
* **Infraestructura actual de despliegue:**
  * Servicio web en Render (`reterminal-pablo`) en plan Free, conectado al repositorio de GitHub del usuario (`pabloalvarez77-ctrl/reterminal-web`).
  * Respaldo de versiones y documentación en Google Drive (carpeta Proyectos Reterminal).
* **Objetivo de la migración a Antigravity:**
  * Centralizar la gestión, refactorización limpia y despliegue del proyecto en Antigravity.
  * Separar la arquitectura monolítica actual (`server.py`) en módulos testeables e independientes.
  * Diseñar una gestión del calendario de Outlook robusta, desacoplada y definitiva.

---

## 2. Especificaciones de Hardware y Diseño Gráfico

### 2.1. Pantalla E-Paper (Spectra 6)
* **Resolución:** 800 × 480 píxeles (orientación horizontal fija).
* **Tecnología:** E-Ink biestable de 6 colores primarios (Spectra 6). La pantalla retiene la imagen estática sin consumir energía.
* **Paleta de color estricta:**
  * `#FFFFFF` (Blanco puro / Fondo base)
  * `#000000` (Negro sólido / Texto principal, líneas estructurales)
  * `#0044CC` (Azul primario / Encabezados, enlaces, lluvia, tiempo transcurrido)
  * `#008833` (Verde / Variación positiva en finanzas, tráfico fluido)
  * `#D60000` (Rojo / Variación negativa en finanzas, tráfico demorado, marcador de hora actual)
  * `#FFCC00` (Amarillo / Sol, luna, rayos de tormenta, ópticas vehiculares)
* **Regla gráfica crítica:** No utilizar colores fuera de esta paleta, gradientes ni suavizados alfa (antialiasing) difuso, ya que el controlador e-paper produce tramados estocásticos (dithering) que degradan la nitidez del texto.

### 2.2. Distribución Espacial del Dashboard (Layout 800×480)
1. **Cabecera superior (px 8 a 74, alto 66px):**
   * **Reloj digital:** Hora en Buenos Aires (UTC-3), formato `HH:MM` en fuente grande azul (`#0044CC`).
   * **Fecha:** Día de la semana y fecha completa en español (`Lunes, 3 de Noviembre`), ubicación Buenos Aires.
   * **Pronóstico fin de semana:** Subpanel central con temperaturas mín/máx e íconos para sábado y domingo.
   * **Clima actual Aeroparque (SABE):** Temperatura en tiempo real, condición e ícono dinámico (sol diurno / luna nocturna).
2. **Panel Izquierdo: Agenda Laboral y Tráfico (px 8 a 448, ancho 440px):**
   * **Encabezado:** `PRÓXIMAS 8 HORAS` y pastilla azul con ventana horaria móvil (ej. `11:56 – 19:56`).
   * **Timeline laboral (8 a 17 hs):** Barra de 8px de alto estilo e-paper con marcas horarias, etiquetas en 8h, 11h, 14h y 17h, bloques negros de citas ocupadas y puntero triangular rojo indicando la hora actual.
   * **Tarjetas de citas (Propuesta A):** Cajas blancas con borde negro y acento azul a la izquierda. Hasta 5 o 6 citas visibles con horario y título al mismo nivel en 13px negrita, pastilla de duración a la derecha (`30m`, `1h`) y segunda línea con ubicación limpia (`Microsoft Teams`, `Sala BITALI`) o asistentes.
   * **Barra inferior de Tráfico / ETA a Casa:** Solo visible de lunes a viernes entre las 15:00 y las 19:00 hs. Dibuja la silueta de una Ford Bronco Sport (verde si el tráfico es fluido, roja si está demorado), duración y hora estimada de llegada a Iberá 3544.
3. **Panel Derecho: Cartera de Finanzas (px 454 a 792, ancho 338px):**
   * **Encabezado:** `GOOGLE FINANCE / CARTERA`.
   * **Cuadrícula de 2 columnas × 3 filas** (6 activos bursátiles).
   * Cada tarjeta incluye ticker, pastilla de variación porcentual del día (verde `▲ +X.XX%` o roja `▼ -X.XX%`), precio actual formateado, minigráfico de velas diarias de 60 días (60D) y nombre de la empresa.

---

## 3. Arquitectura de Módulos y Fuentes de Datos

### 3.1. Calendario de Outlook / Office 365
* **Fuente:** Feed iCalendar publicado de Office 365 Exchange Online (Bitali).
* **Mecanismo de consulta:** `urllib.request` con compresión gzip, cabeceras `Cache-Control: no-cache` y parámetro aleatorio `_cb={timestamp}` contra la CDN de Microsoft Azure.
* **Lógica de interpretación (Two-Pass Resolution):**
  * *Pasada 1:* Escaneo de fechas excluidas (`EXDATE`, `X-MICROSOFT-EXDATE`) y excepciones modificadas/canceladas (`RECURRENCE-ID`).
  * *Pasada 2:* Expansión de recurrencias (`RRULE`) diarias, semanales, mensuales y anuales; filtrado de citas de hoy y ventana de próximas 8 horas.
* **Reglas de exclusión:**
  * Lectura de `PidLidBusyStatus` vía `X-MICROSOFT-CDO-BUSYSTATUS`: se admiten `BUSY`, `OOF`, `WORKINGELSEWHERE`. Se descartan `TENTATIVE` y `FREE`.
  * Filtro de títulos bloqueados: "tiempo de concentración", "focus time".
  * Ventana temporal: Citas que finalicen después de `now - 45 min` y arranquen antes de `now + 8 horas`.

### 3.2. Clima y Meteorología
* **Estación oficial:** Aeroparque Jorge Newbery (SABE, lat -34.5586, lon -58.4164).
* **Proveedor primario:** API abierta de Open-Meteo.
* **Proveedor secundario (failover):** METAR oficial aeronáutico de `aviationweather.gov`.
* **Lógica día/noche en Buenos Aires:** Sobreescritura estricta por horario solar local (7:00 a 19:00 hs sol; 20:00 a 06:00 hs luna) para evitar desfasajes del flag de la API.
* **Mapeo exhaustivo de íconos WMO:** 28 códigos WMO categorizados (sol, nublado, niebla, lluvia, tormenta), sin cláusula `else` ciega a lluvia.

### 3.3. Finanzas e Inversiones
* **Lista de activos (Google Sheets):** Exportación CSV de la hoja `1t1l4MjlXuid0ljh2zZuUC-5mAyHVZyQUrjV-NtfXKx4`. Extrae los primeros 6 tickers (columna A). Normaliza `BRK.B` -> `BRK-B` y `BTC` -> `BTC-USD`.
* **Cotizaciones y velas (Yahoo Finance):** Consulta a `query1.finance.yahoo.com`. Extrae precio, cierre anterior, % de variación y 60 velas diarias OHLC para dibujar minigráficos.

### 3.4. Tráfico y ETA a Casa (Google Maps API)
* **Trayecto:** Bitali (Planta El Talar) -> Iberá 3544, CABA.
* **Ventana de activación:** Lunes a Viernes de 15:00 a 19:00 hs. (De 15 a 17 hs proyecta salida a las 17h; de 17 a 19 hs salida inmediata).
* **Redundancia:** Google Routes API (primaria) y Google Distance Matrix API (respaldo).
* **Criterio de congestión:** Rojo si la duración supera el 25% del tiempo normal estático; verde en caso contrario.

---

## 4. Endpoints HTTP del Servidor
* `GET /dashboard.png` o `/image.png`: Imagen PNG final (800×480) para el buffer del reTerminal.
* `GET /` o `/index.html`: Vista HTML con imagen embebida en base64 para previsualización.
* `GET /api/calendar`: JSON con eventos de la ventana actual.
* `GET /api/finance`: JSON con activos, cotizaciones y velas de 60 días.
* `GET /api/debug_calendar`: Diagnóstico de calendar.ics (VEVENTs, EXDATEs, cancelaciones, UIDs).
* `GET /api/debug_weather`: Diagnóstico del clima (estación, regla día/noche, WMO code, fuente).
* `GET /api/debug_traffic`: Diagnóstico en vivo de Google Maps (duraciones, demoras, errores).

---

## 5. Historial de Problemas Clave y Soluciones
1. **Calendario de Outlook:** Corrección de la sobre-exclusión de citas recurrentes asociando cancelaciones por fecha y hora completa (`uid, rec_ymd` y `uid, rec_ymd_hm`).
2. **Consumo de horas en Render (720 de 750h):** Acotar las consultas del reTerminal / pingers al horario 06:45 a 20:00 hs para reducir el consumo a ~300 h/mes en el plan gratuito.
3. **Mapeo de Clima:** Eliminación de falsos positivos de lluvia en días nublados o con niebla (WMO 45/48).

---

## 6. Objetivos de la Migración a Antigravity
1. **Modularización limpia:** Descomponer `server.py` (~1.900 líneas) en paquetes desacoplados:
   * `core/renderer.py` (dibujo Pillow y paleta Spectra 6).
   * `services/calendar_service.py` (parser RFC 5545 con tests de mock).
   * `services/weather_service.py` (Open-Meteo + METAR).
   * `services/finance_service.py` (Sheets + Yahoo Finance).
   * `services/traffic_service.py` (Google Maps Routes).
2. **Robustez y Testing:** Tests unitarios de casos de borde en iCalendar (UNTIL, COUNT, RECURRENCE-ID, allday).
3. **Despliegue y Recursos:** Contenedor Docker liviano (Render / Fly.io / VPS / Home Assistant local).
4. **Resiliencia en el reTerminal:** Scripts de cliente con reintentos y timeouts ≥60s ante cold-starts de Render.
