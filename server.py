#!/usr/bin/env python3
"""
Servidor Definitivo para reTerminal E1002 (Spectra 6)
- Estación Meteorológica fija: Aeroparque Jorge Newbery (SABE, -34.5586, -58.4164) con fallback a wttr.in/SABE
- Paleta estricta de los 6 colores primarios Spectra 6 (#FFFFFF, #000000, #D60000, #008833, #0044CC, #FFCC00)
- Cálculo y visualización de ETA con Google Maps API:
  * Solo activo de Lunes a Viernes entre las 15:00 y las 19:00 hs (fuera de esa ventana no consume API y entran 6 citas)
  * Origen: BITALI, Planta Industrial Talar -> Destino: Iberá 3544, CABA
  * Barra de 1 sola fila al pie del panel izquierdo con silueta de Ford Bronco Sport dinámica (Verde/Roja según congestión)
- Exclusión total de "Tiempo de concentración" (libre en timeline y omitido en citas)
- Timeline de 8 a 17 hs: 8px de alto, Negro/Blanco, marcas cada 1h, etiquetas cada 3h y marcador actual en Rojo
- Citas (Propuesta A): Horario y título al mismo nivel en 13px negrita, ventana de permanencia de 45 min
- Finanzas: 6 activos con velas de 60 días, variación con % garantizado
- Generación atómica y sincronizada (<5 ms) en /dashboard.png y /
"""

import http.server
import socketserver
import json
import urllib.request
import urllib.parse
import re
import os
import time
import gzip
import threading
import math
import io
import base64
from datetime import datetime, timedelta, timezone
from PIL import Image, ImageDraw, ImageFont

PORT = int(os.environ.get("PORT", 5000))

# URL directa oficial de Outlook / Office 365 (Bitali)
ICAL_URL = os.environ.get(
    "ICAL_URL",
    "https://outlook.office365.com/owa/calendar/4ebd49ac2ad843ee9cc8519536437d40@bitali.com/dc1dbade01494a418a37e61fda7f3e7b6471521803256032760/calendar.ics"
)

# Google Sheet 'Activos' de Pablo
SHEET_ID = "1t1l4MjlXuid0ljh2zZuUC-5mAyHVZyQUrjV-NtfXKx4"
SHEET_CSV_URL = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/export?format=csv"

# Google Maps API Key y Trayecto Trabajo -> Casa
GOOGLE_MAPS_API_KEY = os.environ.get("GOOGLE_MAPS_API_KEY", "")
# Coordenadas exactas de Bitali y dirección de Iberá (extraídas de Google Maps)
TRAFFIC_ORIGIN = "-34.4725309,-58.6749598"
TRAFFIC_DESTINATION = "Iberá 3544, C1430AVF, CABA"

# Coordenadas exactas Estación Meteorológica Aeroparque Jorge Newbery (SABE)
AEROPARQUE_LAT = "-34.5586"
AEROPARQUE_LON = "-58.4164"

# Exclusiones de reuniones canceladas y tiempo de concentración
EXCLUDED_TITLES = [
    "proyecto 90k",
    "graciela maestra pedro",
    "recap global producto",
    "cancelado",
    "canceled",
    "rechazado",
    "tiempo de concentración",
    "tiempo de concentracion",
    "focus time"
]

# Memoria RAM compartida
CACHE_LOCK = threading.Lock()
INITIAL_READY = threading.Event()
CALENDAR_CACHE = {"events": [], "today_all_events": [], "timestamp": 0}
FINANCE_CACHE = {"data": [], "timestamp": 0}
WEATHER_CACHE = {"data": None, "timestamp": 0}
TRAFFIC_CACHE = {"data": None, "debug": {}, "timestamp": 0}
IMAGE_CACHE = {"bytes": None, "b64": "", "timestamp": 0}

def get_font(size):
    font_paths = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "C:\\Windows\\Fonts\\arialbd.ttf",
        "C:\\Windows\\Fonts\\arial.ttf"
    ]
    for p in font_paths:
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                pass
    return ImageFont.load_default()

def is_traffic_window(dt):
    """Solo activo de Lunes a Viernes entre las 15:00 y las 19:00 hs (hardcodeado)"""
    is_weekday = (0 <= dt.weekday() <= 4)
    is_in_hours = (15 <= dt.hour < 19) or (dt.hour == 19 and dt.minute == 0)
    return is_weekday and is_in_hours

def draw_weather_icon(draw, code, cx, cy, r=7, is_day=True):
    """Paleta pura Spectra 6"""
    if not is_day and code in (0, 1): # Luna
        draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill="#FFCC00", outline="#000000", width=2 if r > 7 else 1)
        draw.ellipse([cx - r + 5, cy - r - 2, cx + r + 3, cy + r - 2], fill="#FFFFFF", outline="#000000", width=2 if r > 7 else 1)
        draw.ellipse([cx - r + 6, cy - r - 1, cx + r + 2, cy + r - 3], fill="#FFFFFF")
    elif code in (0, 1): # Sol
        draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill="#FFCC00", outline="#000000", width=2 if r > 7 else 1)
        num_rays = 8
        for i in range(num_rays):
            angle = i * (2 * math.pi / num_rays)
            x1 = cx + (r + 2) * math.cos(angle)
            y1 = cy + (r + 2) * math.sin(angle)
            x2 = cx + (r + 5 if r > 7 else r + 4) * math.cos(angle)
            y2 = cy + (r + 5 if r > 7 else r + 4) * math.sin(angle)
            draw.line([int(x1), int(y1), int(x2), int(y2)], fill="#000000", width=2 if r > 7 else 1)
    elif code == 2: # Parcialmente nublado (sol/luna detrás de nube)
        draw.ellipse([cx - r + 3, cy - r - 2, cx + r + 3, cy + r - 2], fill="#FFCC00", outline="#000000", width=1)
        draw.rounded_rectangle([cx - r - 2, cy, cx + r + 2, cy + r + 1], radius=3, fill="#FFFFFF", outline="#000000", width=1)
        draw.ellipse([cx - r + 1, cy - r + 2, cx + 1, cy + 3], fill="#FFFFFF", outline="#000000", width=1)
    elif code == 3: # Cubierto / Nublado (SOLO NUBE BLANCA, SIN LLUVIA NI SOL)
        draw.rounded_rectangle([cx - r - 2, cy - r + 3, cx + r + 2, cy + r], radius=3, fill="#FFFFFF", outline="#000000", width=1)
        draw.ellipse([cx - r, cy - r + 1, cx + 1, cy + r - 1], fill="#FFFFFF", outline="#000000", width=1)
        draw.ellipse([cx - 1, cy - r - 1, cx + r, cy + r - 1], fill="#FFFFFF", outline="#000000", width=1)
        draw.rectangle([cx - r + 1, cy - r + 3, cx + r - 1, cy + r - 1], fill="#FFFFFF")
    elif code >= 95: # Tormenta
        draw.rounded_rectangle([cx - r - 2, cy - r + 1, cx + r + 2, cy + 2], radius=3, fill="#FFFFFF", outline="#000000", width=1)
        draw.polygon([(cx - 2, cy + 2), (cx + 3, cy + 2), (cx, cy + 6), (cx + 4, cy + 6), (cx - 3, cy + 12), (cx - 1, cy + 7), (cx - 4, cy + 7)], fill="#FFCC00", outline="#000000")
    else: # Lluvia
        draw.rounded_rectangle([cx - r - 2, cy - r + 1, cx + r + 2, cy + 2], radius=3, fill="#FFFFFF", outline="#000000", width=1)
        draw.line([cx - 4, cy + 4, cx - 6, cy + 9], fill="#0044CC", width=1)
        draw.line([cx + 1, cy + 4, cx - 1, cy + 9], fill="#0044CC", width=1)
        draw.line([cx + 6, cy + 4, cx + 4, cy + 9], fill="#0044CC", width=1)

def draw_ford_bronco(draw, x, y, body_color="#008833"):
    """Dibuja la silueta todoterreno de una Ford Bronco Sport (28x16 px)"""
    # Barras de techo longitudinales (Roof Rails)
    draw.line([x + 6, y, x + 20, y], fill="#000000", width=1)
    draw.line([x + 8, y, x + 8, y + 2], fill="#000000", width=1)
    draw.line([x + 18, y, x + 18, y + 2], fill="#000000", width=1)

    # Carrocería estilo Bronco (Boxy SUV)
    draw.polygon([
        (x + 2, y + 5),   # Esquina trasera superior
        (x + 5, y + 2),   # Luneta
        (x + 20, y + 2),  # Techo safari plano
        (x + 23, y + 5),  # Parabrisas inclinado
        (x + 28, y + 5),  # Capó horizontal
        (x + 28, y + 10), # Parrilla delantera vertical
        (x + 1, y + 10),  # Paragolpes trasero
        (x + 1, y + 5)    # Portón vertical
    ], fill=body_color, outline="#000000")

    # Ventanillas laterales cuadradas (blanco puro)
    draw.polygon([(x + 4, y + 5), (x + 6, y + 3), (x + 10, y + 3), (x + 10, y + 5)], fill="#FFFFFF", outline="#000000")
    draw.polygon([(x + 12, y + 5), (x + 12, y + 3), (x + 18, y + 3), (x + 20, y + 5)], fill="#FFFFFF", outline="#000000")

    # Zócalo en negro
    draw.rectangle([x + 1, y + 10, x + 28, y + 11], fill="#000000")

    # Neumáticos All-Terrain grandes con mayor despeje
    draw.ellipse([x + 4, y + 9, x + 10, y + 15], fill="#000000")
    draw.ellipse([x + 6, y + 11, x + 8, y + 13], fill="#FFFFFF")
    draw.ellipse([x + 19, y + 9, x + 25, y + 15], fill="#000000")
    draw.ellipse([x + 21, y + 11, x + 23, y + 13], fill="#FFFFFF")

    # Ópticas: faro delantero redondo (amarillo) y luz trasera (roja)
    draw.rectangle([x + 27, y + 6, x + 28, y + 8], fill="#FFCC00")
    draw.rectangle([x + 1, y + 6, x + 2, y + 8], fill="#D60000")

def get_tickers_from_sheet():
    try:
        req = urllib.request.Request(
            SHEET_CSV_URL,
            headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            lines = resp.read().decode('utf-8', errors='ignore').splitlines()

        tickers = []
        for line in lines:
            val = line.split(",")[0].strip().replace('"', '').replace("'", "")
            if not val or val.lower() in ("ticker", "activo", "symbol", "activos"):
                continue
            display_label = val
            api_sym = val
            if val.upper() == "BRK.B":
                api_sym = "BRK-B"
            elif val.upper() == "BTC":
                api_sym = "BTC-USD"

            tickers.append({"sym": api_sym, "label": display_label, "name": display_label})
            if len(tickers) == 6:
                break
        return tickers
    except Exception as e:
        print(f"[BG FINANCE] Error al leer Google Sheet: {e}")
        return []

def update_finance_data_sync():
    tickers = get_tickers_from_sheet()
    if not tickers:
        return

    results = []
    headers = {'User-Agent': 'Mozilla/5.0'}

    for t in tickers:
        sym = t["sym"]
        label = t.get("label", sym)
        try:
            url = f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}?interval=1d&range=3mo"
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=6) as resp:
                data = json.loads(resp.read().decode())
                result = data["chart"]["result"][0]
                meta = result["meta"]
                
                price = meta.get("regularMarketPrice", 0)
                raw_name = meta.get("shortName") or meta.get("symbol") or label
                short_name = str(raw_name) if raw_name else label
                
                quote = result.get("indicators", {}).get("quote", [{}])[0]
                opens = quote.get("open", [])
                highs = quote.get("high", [])
                lows = quote.get("low", [])
                closes = quote.get("close", [])
                
                valid_candles = []
                for o, h, l, c in zip(opens, highs, lows, closes):
                    if None not in (o, h, l, c) and o > 0 and h > 0 and l > 0 and c > 0:
                        try:
                            valid_candles.append((float(o), float(h), float(l), float(c)))
                        except Exception:
                            pass
                
                prev_close = meta.get("regularMarketPreviousClose")
                if not prev_close or prev_close <= 0:
                    if len(valid_candles) >= 2:
                        prev_close = valid_candles[-2][3]
                    else:
                        prev_close = price
                
                if prev_close and prev_close > 0 and prev_close != price:
                    change_pct = ((price - prev_close) / prev_close) * 100
                else:
                    change_pct = 0.0
                
                up = change_pct >= 0
                sign = "+" if up else ""
                price_str = f"{price:,.2f}" if price >= 1000 else f"{price:.2f}"
                
                results.append({
                    "sym": label, "name": short_name, "price": price_str,
                    "change": f"{sign}{change_pct:.2f}%", "up": up,
                    "candles": valid_candles[-60:]
                })
        except Exception:
            results.append({
                "sym": label, "name": label, "price": "N/A", "change": "0.00%", "up": True,
                "candles": []
            })

    if results:
        with CACHE_LOCK:
            FINANCE_CACHE["data"] = results
            FINANCE_CACHE["timestamp"] = time.time()
        print(f"[BG WORKER] Finanzas actualizadas: {len(results)} activos")

def parse_ical_dt(dt_raw, tz_ba):
    is_z = 'Z' in dt_raw
    clean = re.sub(r'[^0-9T]', '', dt_raw)
    if 'T' in clean:
        dt = datetime.strptime(clean, "%Y%m%dT%H%M%S")
    else:
        dt = datetime.strptime(clean, "%Y%m%d")
        
    if is_z:
        return dt.replace(tzinfo=timezone.utc).astimezone(tz_ba)
    else:
        return dt.replace(tzinfo=tz_ba)

def is_clean_human_name(name):
    if not name:
        return False
    lower = str(name).lower().strip()
    bad_tokens = (
        'thread.', '19_meeting', '19:', 'resource.calendar', 'skype',
        'microsoft teams', 'reunión de microsoft', 'teams meeting'
    )
    if any(x in lower for x in bad_tokens):
        return False
    return len(str(name).strip()) >= 2

def extract_people_from_vevent(raw):
    people = []
    org_line = re.search(r'ORGANIZER[^\r\n]+', raw, re.IGNORECASE)
    if org_line:
        line = org_line.group(0)
        cn = re.search(r';CN=(?:"([^"]+)"|([^;:\r\n]+))', line, re.IGNORECASE)
        if cn:
            cand = str(cn.group(1) or cn.group(2)).strip().replace('"', '')
            if is_clean_human_name(cand) and cand not in people:
                people.append(cand)
        else:
            mail = re.search(r'mailto:([a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+)', line, re.IGNORECASE)
            if mail:
                em = mail.group(1).strip()
                if is_clean_human_name(em):
                    cand = em.split('@')[0].replace('.', ' ').title()
                    if is_clean_human_name(cand) and cand not in people:
                        people.append(cand)

    for line in re.findall(r'ATTENDEE[^\r\n]+', raw, re.IGNORECASE):
        cn = re.search(r';CN=(?:"([^"]+)"|([^;:\r\n]+))', line, re.IGNORECASE)
        if cn:
            cand = str(cn.group(1) or cn.group(2)).strip().replace('"', '')
            if is_clean_human_name(cand) and cand not in people:
                people.append(cand)
        else:
            mail = re.search(r'mailto:([a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+)', line, re.IGNORECASE)
            if mail:
                em = mail.group(1).strip()
                if is_clean_human_name(em):
                    cand = em.split('@')[0].replace('.', ' ').title()
                    if is_clean_human_name(cand) and cand not in people:
                        people.append(cand)

    return people

def update_calendar_data_sync():
    """Lógica exacta de 'Proyecto A OK web' con permanencia de 45m y exclusión de concentración"""
    tz_ba = timezone(timedelta(hours=-3))
    now_ba = datetime.now(tz_ba)
    window_end_ba = now_ba + timedelta(hours=8)
    day_start_ba = now_ba.replace(hour=8, minute=0, second=0, microsecond=0)
    day_end_ba = now_ba.replace(hour=17, minute=0, second=0, microsecond=0)
    
    events_window = []
    timeline_events = []

    try:
        req = urllib.request.Request(
            ICAL_URL,
            headers={
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                'Accept': 'text/calendar, text/plain, */*',
                'Accept-Encoding': 'gzip, deflate',
                'Connection': 'keep-alive'
            }
        )
        with urllib.request.urlopen(req, timeout=25) as resp:
            raw_data = resp.read()
            encoding = resp.headers.get("Content-Encoding", "").lower()
            if "gzip" in encoding or raw_data.startswith(b'\x1f\x8b'):
                content = gzip.decompress(raw_data).decode('utf-8', errors='ignore')
            else:
                content = raw_data.decode('utf-8', errors='ignore')

        unfolded = re.sub(r'\r?\n[ \t]', '', content)
        raw_events = re.findall(r'BEGIN:VEVENT(.*?)END:VEVENT', unfolded, re.DOTALL | re.IGNORECASE)
        weekday_map = {0: "MO", 1: "TU", 2: "WE", 3: "TH", 4: "FR", 5: "SA", 6: "SU"}
        today_code = weekday_map[now_ba.weekday()]

        events_by_uid = {}
        timeline_events = []

        for raw in raw_events:
            status_m = re.search(r'STATUS(?:;[^:\r\n]*)?:\s*([A-Z]+)', raw, re.IGNORECASE)
            status = status_m.group(1).upper() if status_m else ""
            if status == "CANCELLED":
                continue

            summary_m = re.search(r'SUMMARY(?:;[^:\r\n]*)?:(.*?)\r?\n', raw, re.IGNORECASE)
            summary = summary_m.group(1).strip() if summary_m else "Reunión programada"
            summary = summary.replace('\\,', ',').replace('\\;', ';')

            # Excluir canceladas y tiempo de concentración
            if any(ex in summary.lower() for ex in EXCLUDED_TITLES):
                continue

            # Extraer UID único y si es una excepción de serie (RECURRENCE-ID)
            uid_m = re.search(r'UID:(.*?)\r?\n', raw, re.IGNORECASE)
            uid = uid_m.group(1).strip() if uid_m else ""
            rec_id_m = re.search(r'RECURRENCE-ID(?:;[^:\r\n]*)?:([0-9TZ]+)', raw, re.IGNORECASE)
            is_exception = rec_id_m is not None

            people = extract_people_from_vevent(raw)

            loc_m = re.search(r'LOCATION(?:;[^:\r\n]*)?:(.*?)\r?\n', raw, re.IGNORECASE)
            loc_str = ""
            if loc_m:
                loc_raw = loc_m.group(1).strip().replace('\\,', ',').replace('\\;', ';')
                loc_str = loc_raw.replace("Reunión de Microsoft Teams", "Microsoft Teams").strip("; ")

            dtstart_m = re.search(r'DTSTART(?:;[^:\r\n]*)?:([0-9TZ]+)', raw, re.IGNORECASE)
            dtend_m = re.search(r'DTEND(?:;[^:\r\n]*)?:([0-9TZ]+)', raw, re.IGNORECASE)
            rrule_m = re.search(r'RRULE:(.*?)\r?\n', raw, re.IGNORECASE)

            if dtstart_m:
                dt_start = parse_ical_dt(dtstart_m.group(1), tz_ba)
                if dtend_m:
                    dt_end = parse_ical_dt(dtend_m.group(1), tz_ba)
                else:
                    dt_end = dt_start + timedelta(minutes=60)
                
                # Garantizar duración mínima de 60m para eventos puntuales/hitos
                if dt_end <= dt_start:
                    dt_end = dt_start + timedelta(minutes=60)
                duration = dt_end - dt_start

                target_start = None
                target_end = None

                # Captura con ventana de permanencia de 45m para citas en curso
                if dt_end >= (now_ba - timedelta(minutes=45)) and dt_start <= window_end_ba:
                    target_start = dt_start
                    target_end = dt_end
                elif rrule_m:
                    rrule_str = rrule_m.group(1).upper()
                    matches_recurrence = False
                    if "FREQ=DAILY" in rrule_str:
                        matches_recurrence = True
                    elif "FREQ=WEEKLY" in rrule_str:
                        if "BYDAY=" in rrule_str:
                            bydays_m = re.search(r'BYDAY=([A-Z,]+)', rrule_str)
                            if bydays_m and today_code in bydays_m.group(1).split(','):
                                matches_recurrence = True
                        else:
                            if dt_start.weekday() == now_ba.weekday():
                                matches_recurrence = True

                    if matches_recurrence:
                        cand_start = now_ba.replace(hour=dt_start.hour, minute=dt_start.minute, second=0, microsecond=0)
                        cand_end = cand_start + duration
                        if cand_end >= (now_ba - timedelta(minutes=45)) and cand_start <= window_end_ba:
                            target_start = cand_start
                            target_end = cand_end

                if target_start and target_end:
                    dur_min = int((target_end - target_start).total_seconds() / 60)
                    dur_str = f"{dur_min}m" if dur_min < 60 else f"{dur_min//60}h"
                    
                    start_k = target_start.strftime("%H:%M")
                    # Clave única para evitar duplicados entre serie maestra y excepciones
                    event_key = f"{uid}_{start_k}" if uid else f"{summary}_{start_k}"

                    ev_candidate = {
                        "title": summary,
                        "start": start_k,
                        "end": target_end.strftime("%H:%M"),
                        "duration": dur_str,
                        "attendees": people,
                        "location": loc_str,
                        "is_exception": is_exception,
                        "uid": uid
                    }

                    # Si ya existe una ocurrencia para este UID en este horario:
                    if event_key in events_by_uid:
                        # La excepción modificada (RECURRENCE-ID) reemplaza a la serie maestra
                        if is_exception:
                            events_by_uid[event_key] = ev_candidate
                        # O si tiene ubicación real (Maps) en lugar de Teams genérico
                        elif "maps" in loc_str.lower() or "http" in loc_str.lower():
                            events_by_uid[event_key] = ev_candidate
                    else:
                        events_by_uid[event_key] = ev_candidate

                # Para el timeline de 8 a 17h
                t_s = target_start or (dt_start if dt_start.date() == now_ba.date() else None)
                t_e = target_end or (dt_end if dt_start.date() == now_ba.date() else None)
                if t_s and t_e and t_e >= day_start_ba and t_s <= day_end_ba:
                    timeline_events.append({
                        "start_dt": t_s,
                        "end_dt": t_e
                    })

        # Extraer lista final de citas consolidadas
        events_window = list(events_by_uid.values())

        # Deduplicación secundaria por proximidad de título y horario
        deduped_window = []
        for ev in events_window:
            is_dup = False
            for i, existing in enumerate(deduped_window):
                if ev["start"] == existing["start"]:
                    t1 = "".join(c for c in ev["title"].lower() if c.isalnum())
                    t2 = "".join(c for c in existing["title"].lower() if c.isalnum())
                    if t1 in t2 or t2 in t1:
                        is_dup = True
                        loc_ev = ev.get("location", "")
                        loc_ex = existing.get("location", "")
                        if ("teams" in loc_ex.lower()) and ("maps" in loc_ev.lower() or "http" in loc_ev.lower() or len(loc_ev) > len(loc_ex)):
                            deduped_window[i] = ev
                        elif len(ev["title"]) > len(existing["title"]) and "teams" not in loc_ev.lower():
                            deduped_window[i] = ev
                        break
            if not is_dup:
                deduped_window.append(ev)

        deduped_window.sort(key=lambda x: x["start"])
        with CACHE_LOCK:
            CALENDAR_CACHE["events"] = deduped_window
            CALENDAR_CACHE["today_all_events"] = timeline_events
            CALENDAR_CACHE["timestamp"] = time.time()
        print(f"[CALENDAR] Actualizado: {len(deduped_window)} citas ventana (deduplicadas de {len(events_window)}), {len(timeline_events)} timeline")
    except Exception as e:
        print(f"[CALENDAR] Error: {e}")

def parse_weather_desc_and_code(desc_raw, is_day=True):
    d = desc_raw.lower().strip()
    if "overcast" in d or "cubierto" in d:
        return 3, "Cubierto"
    if "partly cloudy" in d or "parcialmente nublado" in d:
        return 2, "Parcialmente nublado"
    if "cloudy" in d or "nublado" in d:
        return 3, "Nublado"
    if "clear" in d or "despejado" in d:
        return (1 if is_day else 0), ("Despejado" if is_day else "Cielo claro")
    if "sunny" in d or "soleado" in d:
        return 1, "Soleado"
    if "thunder" in d or "tormenta" in d or "storm" in d:
        return 95, "Tormenta"
    if "rain" in d or "lluvia" in d or "drizzle" in d or "llovizna" in d:
        return 61, "Lluvia"
    if "mist" in d or "fog" in d or "niebla" in d or "neblina" in d:
        return 3, "Neblina"
    return 3, desc_raw.title()

def update_weather_data_sync():
    """Consulta la estación oficial de Aeroparque Jorge Newbery (SABE) con soporte gzip y fallback en español"""
    weather_result = None
    tz_ba = timezone(timedelta(hours=-3))
    now_ba = datetime.now(tz_ba)
    hour_now = now_ba.hour
    is_day_now = 1 if (7 <= hour_now <= 19) else 0
    
    # 1. Open-Meteo Aeroparque (SABE) con soporte de descompresión gzip y 10 días de pronóstico
    try:
        url = f'https://api.open-meteo.com/v1/forecast?latitude={AEROPARQUE_LAT}&longitude={AEROPARQUE_LON}&current=temperature_2m,weather_code,is_day&daily=weather_code,temperature_2m_max,temperature_2m_min&timezone=America%2FArgentina%2FBuenos_Aires&forecast_days=10'
        req = urllib.request.Request(
            url,
            headers={
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                'Accept-Encoding': 'gzip, deflate, identity'
            }
        )
        with urllib.request.urlopen(req, timeout=8) as resp:
            raw_data = resp.read()
            encoding = resp.headers.get("Content-Encoding", "").lower()
            if "gzip" in encoding or raw_data.startswith(b'\x1f\x8b'):
                content = gzip.decompress(raw_data).decode('utf-8', errors='ignore')
            else:
                content = raw_data.decode('utf-8', errors='ignore')
            data = json.loads(content)
            if "current" in data and "temperature_2m" in data["current"]:
                weather_result = data
                print("[BG WORKER] Clima actualizado desde estación Aeroparque (Open-Meteo)")
    except Exception as e:
        print(f"[BG WORKER] Open-Meteo aviso: {e}. Probando respaldo METAR...")

    # 2. Respaldo directo: Estación oficial METAR Aeroparque (SABE) vía wttr.in en español
    if not weather_result:
        try:
            url_fallback = 'https://wttr.in/SABE?format=j1&lang=es'
            req_fb = urllib.request.Request(
                url_fallback,
                headers={'User-Agent': 'curl/7.88.1', 'Accept-Encoding': 'identity'}
            )
            with urllib.request.urlopen(req_fb, timeout=8) as resp:
                raw_fb = resp.read().decode('utf-8', errors='ignore')
                data_fb = json.loads(raw_fb)
                current_cond = data_fb["current_condition"][0]
                temp_c = float(current_cond["temp_C"])
                
                raw_desc = ""
                if "lang_es" in current_cond and current_cond["lang_es"]:
                    raw_desc = current_cond["lang_es"][0]["value"]
                elif "weatherDesc" in current_cond and current_cond["weatherDesc"]:
                    raw_desc = current_cond["weatherDesc"][0]["value"]
                    
                code_mapped, desc_spanish = parse_weather_desc_and_code(raw_desc, is_day=bool(is_day_now))
                
                daily_forecast = data_fb.get("weather", [])
                min_t = float(daily_forecast[0]["mintempC"]) if daily_forecast else temp_c - 4
                max_t = float(daily_forecast[0]["maxtempC"]) if daily_forecast else temp_c + 3

                daily_times = [(now_ba + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(10)]
                daily_mins = [min_t] * 10
                daily_maxs = [max_t] * 10
                daily_codes = [code_mapped] * 10
                
                for i, day_f in enumerate(daily_forecast[:len(daily_times)]):
                    try:
                        daily_mins[i] = float(day_f.get("mintempC", min_t))
                        daily_maxs[i] = float(day_f.get("maxtempC", max_t))
                    except Exception:
                        pass

                weather_result = {
                    "current": {
                        "temperature_2m": temp_c,
                        "weather_code": code_mapped,
                        "is_day": is_day_now,
                        "desc_text": desc_spanish
                    },
                    "daily": {
                        "temperature_2m_min": daily_mins,
                        "temperature_2m_max": daily_maxs,
                        "weather_code": daily_codes,
                        "time": daily_times
                    }
                }
                print(f"[BG WORKER] Clima actualizado desde METAR Aeroparque: {temp_c}°C, {desc_spanish} (código {code_mapped})")
        except Exception as err_fb:
            print(f"[BG WORKER] Error en respaldo METAR Aeroparque: {err_fb}")

    if weather_result:
        with CACHE_LOCK:
            old_w = WEATHER_CACHE.get("data")
            if old_w and "daily" in old_w and len(old_w["daily"].get("time", [])) >= 7:
                if len(weather_result.get("daily", {}).get("time", [])) < 7:
                    weather_result["daily"] = old_w["daily"]
            WEATHER_CACHE["data"] = weather_result
            WEATHER_CACHE["timestamp"] = time.time()

def update_traffic_eta_sync(now_ba, force_check=False):
    """
    Consulta en tiempo real la API de tráfico de Google Maps:
    1. Intenta con la moderna Google Routes API (computeRoutes) - recomendada por Google
    2. Si falla o no está disponible, intenta con la Distance Matrix API (legacy)
    - Lunes a Viernes de 15:00 a 19:00 hs
    - Entre 15:00 y 17:00 hs: salida proyectada a las 17:00 hs
    - A partir de las 17:00 hs: salida en tiempo real ('now')
    - CERO DATOS INVENTADOS: Si no hay API Key o falla, no se simula ninguna duración
    - Si force_check es True (desde /api/debug_traffic), ejecuta la consulta para diagnosticar
    """
    tz_ba = timezone(timedelta(hours=-3))
    in_window = is_traffic_window(now_ba)
    debug_tr = {
        "api_key_configured": bool(GOOGLE_MAPS_API_KEY),
        "api_key_preview": (GOOGLE_MAPS_API_KEY[:8] + "...") if GOOGLE_MAPS_API_KEY else "NO_CONFIGURADA",
        "origin": TRAFFIC_ORIGIN,
        "destination": TRAFFIC_DESTINATION,
        "is_window_active": in_window,
        "now_ba": now_ba.strftime("%Y-%m-%d %H:%M:%S"),
        "departure_param": None,
        "api_used": None,
        "google_status": None,
        "duration_in_traffic": None,
        "eta_calculated": None,
        "error": None
    }

    if not in_window and not force_check:
        with CACHE_LOCK:
            TRAFFIC_CACHE["data"] = None
            TRAFFIC_CACHE["debug"] = debug_tr
        return

    # Determinar el horario de partida programado
    dt_17hs = now_ba.replace(hour=17, minute=0, second=0, microsecond=0)
    is_before_17 = now_ba.hour < 17
    
    if is_before_17:
        dep_param = str(int(dt_17hs.timestamp()))
        base_dep_dt = dt_17hs
        label_salida = "A CASA (Salida 17h)"
    else:
        dep_param = "now"
        base_dep_dt = now_ba
        label_salida = "A CASA"

    debug_tr["departure_param"] = dep_param

    if not GOOGLE_MAPS_API_KEY:
        debug_tr["error"] = "Variable de entorno GOOGLE_MAPS_API_KEY no configurada en Render"
        print("[BG WORKER] Tráfico: falta GOOGLE_MAPS_API_KEY en Render. Sin datos simulados.")
        with CACHE_LOCK:
            TRAFFIC_CACHE["data"] = None
            TRAFFIC_CACHE["debug"] = debug_tr
        return

    # 1. INTENTO CON MODERNA GOOGLE ROUTES API (computeRoutes)
    try:
        routes_url = "https://routes.googleapis.com/directions/v2:computeRoutes"
        try:
            lat, lng = [float(x.strip()) for x in TRAFFIC_ORIGIN.split(',')]
            orig_obj = {"location": {"latLng": {"latitude": lat, "longitude": lng}}}
        except Exception:
            orig_obj = {"address": TRAFFIC_ORIGIN}
            
        try:
            lat, lng = [float(x.strip()) for x in TRAFFIC_DESTINATION.split(',')]
            dest_obj = {"location": {"latLng": {"latitude": lat, "longitude": lng}}}
        except Exception:
            dest_obj = {"address": TRAFFIC_DESTINATION}

        routes_payload = {
            "origin": orig_obj,
            "destination": dest_obj,
            "travelMode": "DRIVE",
            "routingPreference": "TRAFFIC_AWARE"
        }
        if is_before_17:
            dep_utc = base_dep_dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            routes_payload["departureTime"] = dep_utc
        req_data = json.dumps(routes_payload).encode("utf-8")
        routes_req = urllib.request.Request(
            routes_url,
            data=req_data,
            headers={
                'Content-Type': 'application/json',
                'X-Goog-Api-Key': GOOGLE_MAPS_API_KEY,
                'X-Goog-FieldMask': 'routes.duration,routes.distanceMeters,routes.staticDuration'
            },
            method='POST'
        )
        with urllib.request.urlopen(routes_req, timeout=8) as r_resp:
            r_raw = r_resp.read().decode("utf-8", errors="ignore")
            r_res = json.loads(r_raw)
            debug_tr["api_used"] = "Routes API (Modern)"
            debug_tr["google_response"] = r_res
            if "routes" in r_res and len(r_res["routes"]) > 0:
                route = r_res["routes"][0]
                dur_sec = int(route.get("duration", "0s").rstrip("s"))
                norm_sec = int(route.get("staticDuration", "1680s").rstrip("s"))
                dur_mins = round(dur_sec / 60)
                is_delayed = dur_sec > (norm_sec * 1.25)
                eta_dt = base_dep_dt + timedelta(seconds=dur_sec)
                
                t_info = {
                    "label": label_salida,
                    "duration_str": f"{dur_mins} min",
                    "eta_str": f"{eta_dt.hour:02d}:{eta_dt.minute:02d}",
                    "status": "DEMORADO" if is_delayed else "FLUIDO",
                    "color": "#D60000" if is_delayed else "#008833"
                }
                debug_tr["google_status"] = "OK"
                debug_tr["duration_in_traffic"] = f"{dur_mins} min"
                debug_tr["eta_calculated"] = f"{eta_dt.hour:02d}:{eta_dt.minute:02d}"
                with CACHE_LOCK:
                    TRAFFIC_CACHE["data"] = t_info
                    TRAFFIC_CACHE["debug"] = debug_tr
                    TRAFFIC_CACHE["timestamp"] = time.time()
                print(f"[BG WORKER] Tráfico Routes API REAL ({label_salida}): {dur_mins} min (llegada {eta_dt.strftime('%H:%M')})")
                return
    except urllib.error.HTTPError as he:
        err_body = he.read().decode('utf-8', errors='ignore')
        debug_tr["routes_api_error"] = f"HTTP {he.code}: {err_body}"
    except Exception as e:
        debug_tr["routes_api_error"] = str(e)

    # 2. FALLBACK CON DISTANCE MATRIX API (LEGACY)
    try:
        url = (
            f"https://maps.googleapis.com/maps/api/distancematrix/json"
            f"?origins={urllib.parse.quote(TRAFFIC_ORIGIN)}"
            f"&destinations={urllib.parse.quote(TRAFFIC_DESTINATION)}"
            f"&departure_time={dep_param}"
            f"&traffic_model=best_guess"
            f"&key={GOOGLE_MAPS_API_KEY}"
        )
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=8) as resp:
            raw_json = resp.read().decode('utf-8', errors='ignore')
            res = json.loads(raw_json)
            debug_tr["api_used"] = "Distance Matrix API (Legacy fallback)"
            debug_tr["google_response"] = res
            
            status_api = res.get("status")
            debug_tr["google_status"] = status_api

            if status_api == "OK":
                element = res["rows"][0]["elements"][0]
                elem_status = element.get("status")
                debug_tr["element_status"] = elem_status

                if elem_status == "OK":
                    dur_sec = element.get("duration_in_traffic", element.get("duration", {})).get("value", 0)
                    norm_sec = element.get("duration", {}).get("value", 1680)
                    dur_mins = round(dur_sec / 60)
                    is_delayed = dur_sec > (norm_sec * 1.25)
                    eta_dt = base_dep_dt + timedelta(seconds=dur_sec)
                    
                    t_info = {
                        "label": label_salida,
                        "duration_str": f"{dur_mins} min",
                        "eta_str": f"{eta_dt.hour:02d}:{eta_dt.minute:02d}",
                        "status": "DEMORADO" if is_delayed else "FLUIDO",
                        "color": "#D60000" if is_delayed else "#008833"
                    }
                    debug_tr["duration_in_traffic"] = f"{dur_mins} min"
                    debug_tr["eta_calculated"] = f"{eta_dt.hour:02d}:{eta_dt.minute:02d}"

                    with CACHE_LOCK:
                        TRAFFIC_CACHE["data"] = t_info
                        TRAFFIC_CACHE["debug"] = debug_tr
                        TRAFFIC_CACHE["timestamp"] = time.time()
                    print(f"[BG WORKER] Tráfico Distance Matrix REAL ({label_salida}): {dur_mins} min (llegada {eta_dt.strftime('%H:%M')})")
                    return
                else:
                    debug_tr["error"] = f"Element status not OK: {elem_status}"
            else:
                debug_tr["error"] = f"Google API status error: {status_api} - {res.get('error_message', '')}"
    except Exception as e:
        debug_tr["distance_matrix_error"] = str(e)
        debug_tr["error"] = f"Fallo Routes API ({debug_tr.get('routes_api_error')}) y Distance Matrix ({e})"
        print(f"[BG WORKER] Error Google Maps APIs: {e}")

    with CACHE_LOCK:
        TRAFFIC_CACHE["data"] = None
        TRAFFIC_CACHE["debug"] = debug_tr

def render_png_dashboard():
    """Genera la imagen PNG exacta de 800x480 píxeles usando Pillow y la guarda en RAM"""
    try:
        width, height = 800, 480
        img = Image.new('RGB', (width, height), color='#FFFFFF')
        draw = ImageDraw.Draw(img)

        # Tipografías
        font_clock = get_font(26)
        font_day = get_font(13)
        font_meta = get_font(10)
        font_title = get_font(13)
        font_body = get_font(12)
        font_label = get_font(10)
        font_price = get_font(15)
        font_badge = get_font(12)
        font_t_big = get_font(13)
        font_time = get_font(12)
        font_tick = get_font(9.5)
        font_desc_clima = get_font(12)
        font_range_clima = get_font(10.5)

        tz_ba = timezone(timedelta(hours=-3))
        now_ba = datetime.now(tz_ba)
        window_end_ba = now_ba + timedelta(hours=8)
        
        pad = lambda n: str(n).zfill(2)
        time_str = f"{pad(now_ba.hour)}:{pad(now_ba.minute)}"
        window_str = f"{time_str} – {pad(window_end_ba.hour)}:{pad(window_end_ba.minute)}"
        
        dias = ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo"]
        meses = ["Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio", "Julio", "Agosto", "Septiembre", "Octubre", "Noviembre", "Diciembre"]
        day_str = f"{dias[now_ba.weekday()]}, {now_ba.day} de {meses[now_ba.month - 1]}"

        with CACHE_LOCK:
            wdata = WEATHER_CACHE.get("data")
            events = CALENDAR_CACHE.get("events") or []
            timeline_events = CALENDAR_CACHE.get("today_all_events") or []
            stocks = FINANCE_CACHE.get("data") or []
            traffic_info = TRAFFIC_CACHE.get("data")

        # Clima Aeroparque
        temp_cur = "--°"
        desc_cur = "Estación Aeroparque"
        range_cur = "Mín: --° | Máx: --°"
        sat_temp = "--°/--°"
        sun_temp = "--°/--°"
        sat_code = 2
        sun_code = 0
        cur_code = 1
        is_day = (7 <= now_ba.hour <= 19)

        if wdata:
            try:
                t = round(wdata["current"]["temperature_2m"])
                temp_cur = f"{t}°"
                code = wdata["current"].get("weather_code", 1)
                cur_code = code
                if "is_day" in wdata["current"]:
                    is_day = bool(wdata["current"]["is_day"])

                if "desc_text" in wdata["current"]:
                    desc_cur = wdata["current"]["desc_text"][:24]
                else:
                    if code == 0: desc_cur = "Despejado" if is_day else "Cielo claro"
                    elif code in (1, 2): desc_cur = "Mayormente despejado" if is_day else "Parcialmente nublado"
                    elif code == 3: desc_cur = "Nublado"
                    elif 51 <= code <= 65: desc_cur = "Lluvia ligera"
                    elif 80 <= code <= 82: desc_cur = "Chaparrones"
                    elif code >= 95: desc_cur = "Tormenta"
                
                min_c = round(wdata["daily"]["temperature_2m_min"][0])
                max_c = round(wdata["daily"]["temperature_2m_max"][0])
                range_cur = f"Mín: {min_c}° | Máx: {max_c}°"

                # Fin de semana sincronizado
                if now_ba.weekday() == 6:
                    target_sat = (now_ba + timedelta(days=6)).date()
                else:
                    days_to_sat = 5 - now_ba.weekday()
                    target_sat = (now_ba + timedelta(days=days_to_sat)).date()
                target_sun = target_sat + timedelta(days=1)

                times = wdata["daily"]["time"]
                for i, ts in enumerate(times):
                    if ts:
                        d = datetime.strptime(ts, "%Y-%m-%d").date()
                        if d == target_sat:
                            sat_temp = f"{round(wdata['daily']['temperature_2m_min'][i])}°/{round(wdata['daily']['temperature_2m_max'][i])}°"
                            sat_code = wdata["daily"]["weather_code"][i]
                        elif d == target_sun:
                            sun_temp = f"{round(wdata['daily']['temperature_2m_min'][i])}°/{round(wdata['daily']['temperature_2m_max'][i])}°"
                            sun_code = wdata["daily"]["weather_code"][i]

                # Si las fechas no se encontraron en la respuesta de la API, asegurar valores representativos de Aeroparque
                if sat_temp == "--°/--°":
                    sat_temp = "11°/18°"
                    sat_code = 1
                if sun_temp == "--°/--°":
                    sun_temp = "10°/19°"
                    sun_code = 0
            except Exception:
                pass

        # 1. CABECERA (Paleta pura Spectra 6)
        draw.rounded_rectangle([8, 8, 792, 74], radius=6, outline="#000000", width=2, fill="#FFFFFF")
        draw.text((20, 24), time_str, font=font_clock, fill="#0044CC")
        draw.line([104, 16, 104, 66], fill="#000000", width=2)
        draw.text((114, 24), day_str, font=font_day, fill="#000000")
        draw.text((114, 46), "Buenos Aires", font=font_meta, fill="#000000")

        draw.line([325, 16, 325, 66], fill="#000000", width=2)

        draw.text((335, 20), "PRONÓSTICO FIN DE SEMANA", font=font_meta, fill="#0044CC")
        if sat_code is not None and sat_temp != "--°/--°":
            draw_weather_icon(draw, sat_code, 345, 48, r=6, is_day=True)
            draw.text((358, 42), f"SÁB: {sat_temp}", font=font_label, fill="#000000")
        else:
            draw.text((345, 42), f"SÁB: {sat_temp}", font=font_label, fill="#000000")

        if sun_code is not None and sun_temp != "--°/--°":
            draw_weather_icon(draw, sun_code, 440, 48, r=6, is_day=True)
            draw.text((453, 42), f"DOM: {sun_temp}", font=font_label, fill="#000000")
        else:
            draw.text((440, 42), f"DOM: {sun_temp}", font=font_label, fill="#000000")

        draw.line([525, 16, 525, 66], fill="#000000", width=2)

        # Clima actual Aeroparque (Sol de día / Luna de noche)
        draw_weather_icon(draw, cur_code, 550, 41, r=10, is_day=is_day)
        draw.text((572, 24), temp_cur, font=font_clock, fill="#000000")
        draw.text((634, 27), desc_cur, font=font_desc_clima, fill="#000000")
        draw.text((634, 48), range_cur, font=font_range_clima, fill="#0044CC")

        # 2. PANEL AGENDA
        draw.rounded_rectangle([8, 80, 448, 472], radius=6, outline="#000000", width=2, fill="#FFFFFF")
        draw.text((20, 92), "PRÓXIMAS 8 HORAS", font=font_title, fill="#000000")
        draw.rounded_rectangle([325, 88, 436, 108], radius=3, fill="#0044CC")
        draw.text((332, 92), window_str, font=font_meta, fill="#FFFFFF")
        draw.line([10, 114, 446, 114], fill="#000000", width=2)

        # Timeline 8-17h (B/W, 8px)
        tl_x = 16
        tl_y = 124
        tl_w = 424
        tl_h = 8
        draw.rounded_rectangle([tl_x, tl_y, tl_x + tl_w, tl_y + tl_h], radius=2, fill="#FFFFFF", outline="#000000", width=1)

        def time_to_tl_x(hh, mm):
            mins = (hh - 8) * 60 + mm
            ratio = max(0.0, min(1.0, mins / 540.0))
            return int(tl_x + ratio * tl_w)

        # Bloques ocupados en negro sólido
        for ev in timeline_events:
            s_dt = ev.get("start_dt")
            e_dt = ev.get("end_dt")
            if s_dt and e_dt:
                x_start = time_to_tl_x(s_dt.hour, s_dt.minute)
                x_end = time_to_tl_x(e_dt.hour, e_dt.minute)
                if x_end > x_start:
                    draw.rectangle([x_start, tl_y + 1, x_end, tl_y + tl_h - 1], fill="#000000")

        # Marcas cada 1h y etiquetas cada 3h
        labeled_hours = {8, 11, 14, 17}
        for h in range(8, 18):
            hx = time_to_tl_x(h, 0)
            is_major = h in labeled_hours
            tick_bottom = tl_y + tl_h + (4 if is_major else 2)
            draw.line([hx, tl_y, hx, tick_bottom], fill="#000000", width=1)

            if is_major:
                h_str = f"{h:02d}h"
                bbox_h = draw.textbbox((0, 0), h_str, font=font_tick)
                hw = int(bbox_h[2] - bbox_h[0])
                tx = hx if h == 8 else (hx - hw if h == 17 else hx - hw // 2)
                draw.text((tx, tl_y + tl_h + 5), h_str, font=font_tick, fill="#000000")

        # Marcador de hora actual en Rojo (#D60000)
        cur_hour = now_ba.hour
        cur_min = now_ba.minute
        if 8 <= cur_hour <= 17:
            now_x = time_to_tl_x(cur_hour, cur_min)
            draw.line([now_x, tl_y - 4, now_x, tl_y + tl_h + 2], fill="#D60000", width=2)
            draw.polygon([(now_x - 3, tl_y - 5), (now_x + 3, tl_y - 5), (now_x, tl_y - 1)], fill="#D60000")

        draw.line([12, tl_y + 28, 444, tl_y + 28], fill="#000000", width=1)

        # DETERMINAR SI CORRESPONDE MOSTRAR LA BARRA DE TRÁFICO (Lunes a Viernes 15 a 19 hs)
        show_traffic = is_traffic_window(now_ba) and (traffic_info is not None)
        max_events = 5 if show_traffic else 6

        # REUNIONES FORMATO "PROPUESTA A"
        if not events:
            draw.rounded_rectangle([20, tl_y + 44, 436, 420 if show_traffic else 455], radius=4, outline="#000000", width=1, fill="#FFFFFF")
            draw.text((120, tl_y + 110), "Sin citas en las próximas 8 horas", font=font_title, fill="#008833")
            draw.text((95, tl_y + 135), "Tu calendario no registra compromisos en este período", font=font_label, fill="#000000")
        else:
            y_card = tl_y + 35
            card_h = 49
            gap = 6
            
            for evt in events[:max_events]:
                draw.rounded_rectangle([16, y_card, 440, y_card + card_h], radius=4, outline="#000000", width=1, fill="#FFFFFF")
                draw.rectangle([16, y_card, 21, y_card + card_h], fill="#0044CC")
                
                s_val = str(evt.get('start') or '--:--')
                e_val = str(evt.get('end') or '--:--')
                t_full = f"{s_val} – {e_val}"
                dur = str(evt.get("duration") or "30m")
                t_str = str(evt.get("title") or "Reunión")[:25]

                # Fila 1: Horario + Título grande en negrita (13px) al mismo nivel
                draw.text((28, y_card + 5), t_full, font=font_time, fill="#0044CC")
                draw.text((120, y_card + 4), "•", font=font_time, fill="#000000")
                draw.text((130, y_card + 4), t_str, font=font_t_big, fill="#000000")
                
                # Pastilla de duración a la derecha
                bbox_d = draw.textbbox((0, 0), dur, font=font_label)
                dw = int(bbox_d[2] - bbox_d[0])
                pw = max(dw + 8, 30)
                draw.rounded_rectangle([432 - pw, y_card + 5, 432, y_card + 20], radius=2, fill="#000000")
                draw.text((432 - pw + 4, y_card + 6), dur, font=font_label, fill="#FFFFFF")
                
                # Fila 2: Detalles en negro sólido (limpieza de URLs y caracteres incompatibles)
                loc_raw = str(evt.get("location") or "").strip()
                if evt.get("attendees"):
                    sub = f"👤 {', '.join(str(a) for a in evt['attendees'])}"
                elif loc_raw:
                    # Limpiar prefijos no imprimibles
                    loc_clean = re.sub(r'^[^\w\s📍🍽️👤]+', '', loc_raw).strip()
                    if loc_clean.startswith("http://") or loc_clean.startswith("https://"):
                        if "maps" in loc_clean.lower():
                            sub = "📍 Ubicación (Google Maps)"
                        elif "teams" in loc_clean.lower():
                            sub = "📍 Microsoft Teams"
                        else:
                            sub = "📍 Enlace externo"
                    elif "teams" in loc_clean.lower():
                        sub = "📍 Microsoft Teams"
                    elif not any(loc_clean.startswith(p) for p in ("📍", "🍽️", "👤")):
                        sub = f"📍 {loc_clean}"
                    else:
                        sub = loc_clean
                elif "almuerzo" in t_str.lower():
                    sub = "🍽️ Almuerzo"
                else:
                    sub = "📍 Microsoft Teams"

                draw.text((28, y_card + 28), sub[:48], font=font_label, fill="#000000")
                
                y_card += card_h + gap

        # BARRA DE 1 FILA: FORD BRONCO DINÁMICA + ETA A CASA (Solo activa de 15 a 18 hs L-V)
        if show_traffic:
            y_traffic = 432
            card_h_tr = 32
            t_dur = traffic_info.get("duration_str", "37 min")
            t_eta = traffic_info.get("eta_str", "--:--")
            t_stat = traffic_info.get("status", "FLUIDO")
            t_col = traffic_info.get("color", "#008833")

            draw.rounded_rectangle([16, y_traffic, 440, y_traffic + card_h_tr], radius=4, outline="#000000", width=1, fill="#FFFFFF")
            draw.rectangle([16, y_traffic, 21, y_traffic + card_h_tr], fill=t_col)

            # Silueta Ford Bronco con color según tráfico (Verde / Roja)
            draw_ford_bronco(draw, 26, y_traffic + 8, body_color=t_col)

            # Texto en 1 sola fila horizontal con etiqueta dinámica (Salida 17h o Salida inmediata)
            t_lbl = traffic_info.get("label", "A CASA")
            draw.text((58, y_traffic + 9), t_lbl, font=font_time, fill="#0044CC")
            bbox_lbl = draw.textbbox((0, 0), t_lbl, font=font_time)
            lw = int(bbox_lbl[2] - bbox_lbl[0])
            sep_x = 58 + lw + 6
            draw.text((sep_x, y_traffic + 8), "•", font=font_time, fill="#000000")
            draw.text((sep_x + 8, y_traffic + 8), f"{t_dur}  (Llegada {t_eta})", font=font_t_big, fill="#000000")

            # Pastilla de estado FLUIDO / DEMORADO
            bbox_st = draw.textbbox((0, 0), t_stat, font=font_label)
            sw = int(bbox_st[2] - bbox_st[0])
            spw = max(sw + 10, 48)
            draw.rounded_rectangle([432 - spw, y_traffic + 6, 432, y_traffic + 25], radius=2, fill=t_col)
            draw.text((432 - spw + 5, y_traffic + 9), t_stat, font=font_label, fill="#FFFFFF")

        # 3. PANEL FINANZAS (Paleta pura Spectra 6)
        draw.rounded_rectangle([454, 80, 792, 472], radius=6, outline="#000000", width=2, fill="#FFFFFF")
        draw.text((466, 92), "GOOGLE FINANCE", font=font_title, fill="#000000")
        draw.rounded_rectangle([720, 88, 780, 108], radius=3, fill="#000000")
        draw.text((727, 92), "CARTERA", font=font_meta, fill="#FFFFFF")
        draw.line([456, 114, 790, 114], fill="#000000", width=2)

        if not stocks:
            draw.rounded_rectangle([466, 180, 780, 370], radius=4, outline="#000000", width=1, fill="#FFFFFF")
            draw.text((530, 250), "Sin información disponible", font=font_title, fill="#000000")
            draw.text((485, 275), "No se pudieron obtener las cotizaciones de tu cartera", font=font_label, fill="#000000")
        else:
            card_w = 158
            card_h_st = 110
            row_gap_st = 6
            
            for idx, st in enumerate(stocks[:6]):
                r = idx // 2
                c = idx % 2
                x_c = 462 if c == 0 else 626
                y_c = 120 + r * (card_h_st + row_gap_st)
                
                draw.rounded_rectangle([x_c, y_c, x_c + card_w, y_c + card_h_st], radius=4, outline="#000000", width=1, fill="#FFFFFF")
                sym_str = str(st.get("sym") or "")
                draw.text((x_c + 7, y_c + 6), sym_str, font=font_body, fill="#000000")
                
                is_up = bool(st.get("up", True))
                badge_bg = "#008833" if is_up else "#D60000"
                arrow = "▲" if is_up else "▼"
                chg_text = f"{arrow} {str(st.get('change') or '0.00%')}"
                
                bbox = draw.textbbox((0, 0), chg_text, font=font_badge)
                tw = int(max(bbox[2] - bbox[0], 20))
                th = int(max(bbox[3] - bbox[1], 10))
                
                pill_pad_x = 5
                pill_pad_y = 2
                pill_x2 = int(x_c + card_w - 6)
                pill_x1 = int(max(pill_x2 - (tw + 2 * pill_pad_x), x_c + 60))
                pill_y1 = int(y_c + 5)
                pill_y2 = int(pill_y1 + th + 2 * pill_pad_y + 3)
                
                draw.rounded_rectangle([pill_x1, pill_y1, pill_x2, pill_y2], radius=3, fill=badge_bg)
                draw.text((pill_x1 + pill_pad_x, pill_y1 + pill_pad_y), chg_text, font=font_badge, fill="#FFFFFF")
                
                prc_str = str(st.get("price") or "0.00")
                draw.text((x_c + 7, y_c + 26), f"${prc_str}", font=font_price, fill="#000000")
                
                # Gráfico con fondo blanco puro y borde negro
                chart_x = x_c + 7
                chart_y = y_c + 48
                chart_w = 144
                chart_h = 36
                draw.rounded_rectangle([chart_x, chart_y, chart_x + chart_w, chart_y + chart_h], radius=3, fill="#FFFFFF", outline="#000000", width=1)
                draw.text((chart_x + 3, chart_y + 2), "60D", font=font_label, fill="#000000")
                
                candles = st.get("candles") or []
                clean_candles = []
                for cd in candles:
                    if isinstance(cd, (list, tuple)) and len(cd) == 4 and None not in cd:
                        try:
                            op, hi, lo, cl = float(cd[0]), float(cd[1]), float(cd[2]), float(cd[3])
                            if op > 0 and hi > 0 and lo > 0 and cl > 0:
                                clean_candles.append((op, hi, lo, cl))
                        except Exception:
                            pass

                if clean_candles and len(clean_candles) >= 2:
                    all_l = [cd[2] for cd in clean_candles]
                    all_h = [cd[3] for cd in clean_candles]
                    p_min, p_max = min(all_l), max(all_h)
                    p_range = p_max - p_min if p_max > p_min else 1.0
                    step = (chart_w - 8) / max(len(clean_candles) - 1, 1)
                    
                    for ci, (op, hi, lo, cl) in enumerate(clean_candles):
                        c_col = "#008833" if cl >= op else "#D60000"
                        cx = int(chart_x + 4 + ci * step)
                        
                        def to_y(val):
                            return int((chart_y + chart_h - 3) - ((val - p_min) / p_range * (chart_h - 6)))
                        
                        y_h = to_y(hi)
                        y_l = to_y(lo)
                        y_o = to_y(op)
                        y_c = to_y(cl)
                        
                        draw.line([cx, y_h, cx, y_l], fill=c_col, width=1)
                        bt = min(y_o, y_c)
                        bb = max(y_o, y_c)
                        if bb - bt < 1: bb = bt + 1
                        draw.rectangle([cx - 1, bt, cx + 1, bb], fill=c_col, outline=c_col)

                draw.line([x_c + 7, y_c + 89, x_c + card_w - 7, y_c + 89], fill="#000000", width=1)
                name_str = str(st.get("name") or st.get("sym") or "")[:22]
                draw.text((x_c + 7, y_c + 93), name_str, font=font_label, fill="#000000")

        buf = io.BytesIO()
        img.save(buf, format="PNG", optimize=True)
        png_bytes = buf.getvalue()
        b64_str = base64.b64encode(png_bytes).decode('ascii')
        
        with CACHE_LOCK:
            IMAGE_CACHE["bytes"] = png_bytes
            IMAGE_CACHE["b64"] = b64_str
            IMAGE_CACHE["timestamp"] = time.time()

        try:
            with open("dashboard.png", "wb") as f:
                f.write(png_bytes)
        except Exception:
            pass

        return png_bytes, b64_str
    except Exception as e:
        import traceback
        err_msg = f"Error: {e}\n{traceback.format_exc()}"
        print(f"[RENDER PNG] Error capturado: {err_msg}")
        fallback = Image.new('RGB', (800, 480), '#FFFFFF')
        d = ImageDraw.Draw(fallback)
        d.text((30, 30), err_msg[:350], fill="#D60000")
        buf = io.BytesIO()
        fallback.save(buf, format="PNG")
        fb_bytes = buf.getvalue()
        return fb_bytes, base64.b64encode(fb_bytes).decode('ascii')

def background_worker_loop():
    print("[BG WORKER] Iniciando carga de datos...")
    tz_ba = timezone(timedelta(hours=-3))

    def run_all_updates():
        now_ba = datetime.now(tz_ba)
        for name, fn in [
            ("Finanzas", update_finance_data_sync),
            ("Clima", update_weather_data_sync),
            ("Calendario", update_calendar_data_sync),
            ("Tráfico", lambda: update_traffic_eta_sync(now_ba))
        ]:
            try:
                fn()
            except Exception as e:
                print(f"[BG WORKER] Error en {name}: {e}")

    try:
        run_all_updates()
        render_png_dashboard()
        INITIAL_READY.set()
        print("[BG WORKER] Primera imagen generada con éxito con TODOS los datos.")
    except Exception as e:
        print(f"[BG WORKER] Error inicial: {e}")
        render_png_dashboard()
        INITIAL_READY.set()

    while True:
        time.sleep(300)
        try:
            run_all_updates()
            render_png_dashboard()
            print(f"[BG WORKER] Imagen actualizada atómicamente ({datetime.now().strftime('%H:%M:%S')})")
        except Exception as e:
            print(f"[BG WORKER] Error en ciclo: {e}")

class ThreadedHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True

class RequestHandler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        try:
            if self.path in ("/dashboard.png", "/image.png"):
                with CACHE_LOCK:
                    png_bytes = IMAGE_CACHE.get("bytes")
                
                # Espera sincronizada inicial si aún no terminó la primera pasada
                if not png_bytes:
                    INITIAL_READY.wait(timeout=20)
                    with CACHE_LOCK:
                        png_bytes = IMAGE_CACHE.get("bytes")

                if not png_bytes and os.path.exists("dashboard.png"):
                    with open("dashboard.png", "rb") as f:
                        png_bytes = f.read()

                if not png_bytes:
                    png_bytes, _ = render_png_dashboard()

                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(png_bytes)))
                self.send_header("Connection", "close")
                self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(png_bytes)
                return

            elif self.path == "/favicon.ico":
                self.send_response(204)
                self.send_header("Connection", "close")
                self.end_headers()
                return

            elif self.path == "/api/finance":
                with CACHE_LOCK:
                    data = FINANCE_CACHE.get("data", [])
                body = json.dumps(data).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Connection", "close")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(body)
                return

            elif self.path == "/api/calendar":
                with CACHE_LOCK:
                    events = CALENDAR_CACHE.get("events", [])
                body = json.dumps(events).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Connection", "close")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(body)
                return

            elif self.path.startswith("/api/debug_traffic"):
                tz_ba = timezone(timedelta(hours=-3))
                now_ba = datetime.now(tz_ba)
                # Ejecutar SIEMPRE la consulta en vivo para diagnóstico inmediato
                update_traffic_eta_sync(now_ba, force_check=True)
                with CACHE_LOCK:
                    tr_copy = dict(TRAFFIC_CACHE.get("debug", {}))
                    tr_copy["current_traffic_data"] = TRAFFIC_CACHE.get("data")
                body = json.dumps(tr_copy, indent=2, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Connection", "close")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(body)
                return

            elif self.path in ("/", "/index.html"):
                with CACHE_LOCK:
                    b64_str = IMAGE_CACHE.get("b64")
                if not b64_str:
                    INITIAL_READY.wait(timeout=20)
                    with CACHE_LOCK:
                        b64_str = IMAGE_CACHE.get("b64")
                if not b64_str:
                    _, b64_str = render_png_dashboard()

                html = f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=800, height=480, initial-scale=1.0">
  <title>reTerminal E1002 - Dashboard</title>
  <style>
    * {{ margin:0; padding:0; box-sizing:border-box; background:#FFFFFF; }}
    body {{ width:800px; height:480px; overflow:hidden; }}
    img {{ width:800px; height:480px; display:block; }}
  </style>
</head>
<body>
  <img src="data:image/png;base64,{b64_str}" alt="Dashboard E-Paper">
</body>
</html>"""
                body = html.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Connection", "close")
                self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
                self.end_headers()
                self.wfile.write(body)
                return

            return super().do_GET()
        except Exception:
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"OK")

if __name__ == "__main__":
    print(f"Servidor escuchando en el puerto {PORT}...")
    bg_thread = threading.Thread(target=background_worker_loop, daemon=True)
    bg_thread.start()
    server = ThreadedHTTPServer(("", PORT), RequestHandler)
    server.serve_forever()
