#!/usr/bin/env python3
"""
Servidor Definitivo para reTerminal E1002 (Spectra 6)
- Generación automática de imagen estática PNG (800x480 px) cada 15 minutos en segundo plano
- Cabecera: Más espacio para la descripción del clima (eliminado 'BUENOS AIRES' redundante a la derecha)
- Timeline de 8 a 17 hs: Altura 8px (mitad), negro (ocupado) y blanco (libre), marcas cada 1h, etiquetas cada 3h y marcador de hora actual en ROJO (#D60000)
- Citas (Propuesta A): Horario y título al mismo nivel con fuente ampliada (13px negrita)
- Finanzas: 6 activos con velas de 60 días, variación diaria con signo % garantizado y protección contra nulos
- Entrega inmediata en /dashboard.png y / con imagen Base64 incrustada
"""

import http.server
import socketserver
import json
import urllib.request
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

# Exclusiones de canceladas
EXCLUDED_TITLES = ["proyecto 90k", "graciela maestra pedro", "cancelado", "canceled", "rechazado"]

# Memoria RAM compartida
CACHE_LOCK = threading.Lock()
CALENDAR_CACHE = {"events": [], "today_all_events": [], "debug": {}, "timestamp": 0}
FINANCE_CACHE = {"data": [], "timestamp": 0}
WEATHER_CACHE = {"data": None, "timestamp": 0}
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

def draw_weather_icon(draw, code, cx, cy, r=7):
    """Dibuja íconos vectoriales con contorno negro nítido de alto contraste (grosores enteros)"""
    if code in (0, 1): # Sol despejado
        draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill="#FFCC00", outline="#000000", width=2 if r > 7 else 1)
        num_rays = 8
        for i in range(num_rays):
            angle = i * (2 * math.pi / num_rays)
            x1 = cx + (r + 2) * math.cos(angle)
            y1 = cy + (r + 2) * math.sin(angle)
            x2 = cx + (r + 5 if r > 7 else r + 4) * math.cos(angle)
            y2 = cy + (r + 5 if r > 7 else r + 4) * math.sin(angle)
            draw.line([int(x1), int(y1), int(x2), int(y2)], fill="#000000", width=2 if r > 7 else 1)
    elif code in (2, 3): # Nubes con sol
        draw.ellipse([cx - r + 3, cy - r - 2, cx + r + 3, cy + r - 2], fill="#FFCC00", outline="#000000", width=1)
        draw.rounded_rectangle([cx - r - 2, cy, cx + r + 2, cy + r + 1], radius=3, fill="#FFFFFF", outline="#000000", width=1)
        draw.ellipse([cx - r + 1, cy - r + 2, cx + 1, cy + 3], fill="#FFFFFF", outline="#000000", width=1)
    else: # Lluvia (grosor entero width=1)
        draw.rounded_rectangle([cx - r - 2, cy - r + 1, cx + r + 2, cy + 2], radius=3, fill="#FFFFFF", outline="#000000", width=1)
        draw.line([cx - 4, cy + 4, cx - 6, cy + 9], fill="#0044CC", width=1)
        draw.line([cx + 1, cy + 4, cx - 1, cy + 9], fill="#0044CC", width=1)
        draw.line([cx + 6, cy + 4, cx + 4, cy + 9], fill="#0044CC", width=1)

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
        print("[BG FINANCE] No se obtuvieron tickers de la hoja.")
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
        except Exception as err:
            results.append({
                "sym": label, "name": label, "price": "N/A", "change": "0.00%", "up": True,
                "candles": []
            })

    if results:
        with CACHE_LOCK:
            FINANCE_CACHE["data"] = results
            FINANCE_CACHE["timestamp"] = time.time()
        print(f"[BG WORKER] Finanzas actualizadas: {len(results)} activos con velas 60D")

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
    tz_ba = timezone(timedelta(hours=-3))
    now_ba = datetime.now(tz_ba)
    window_end_ba = now_ba + timedelta(hours=8)
    day_start_ba = now_ba.replace(hour=8, minute=0, second=0, microsecond=0)
    day_end_ba = now_ba.replace(hour=17, minute=0, second=0, microsecond=0)
    
    events_window = []
    events_today_daytime = []

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

        for raw in raw_events:
            status_m = re.search(r'STATUS(?:;[^:\r\n]*)?:\s*([A-Z]+)', raw, re.IGNORECASE)
            status = status_m.group(1).upper() if status_m else ""
            busy_m = re.search(r'X-MICROSOFT-CDO-BUSYSTATUS:\s*([A-Z]+)', raw, re.IGNORECASE)
            busy_status = busy_m.group(1).upper() if busy_m else ""

            if status == "CANCELLED":
                continue

            summary_m = re.search(r'SUMMARY(?:;[^:\r\n]*)?:(.*?)\r?\n', raw, re.IGNORECASE)
            summary = summary_m.group(1).strip() if summary_m else "Reunión programada"
            summary = summary_m.replace('\\,', ',').replace('\\;', ';')

            if any(ex in summary.lower() for ex in EXCLUDED_TITLES):
                continue

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
                    dt_end = dt_start + timedelta(minutes=30)
                duration = dt_end - dt_start

                target_start = None
                target_end = None

                # Evento normal (cualquier cita que solape con la ventana o el día)
                if not rrule_m:
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
                        target_start = cand_start
                        target_end = cand_end

                if target_start and target_end:
                    dur_min = int((target_end - target_start).total_seconds() / 60)
                    dur_str = f"{dur_min}m" if dur_min < 60 else f"{dur_min//60}h"
                    
                    event_dict = {
                        "title": summary,
                        "start": target_start.strftime("%H:%M"),
                        "end": target_end.strftime("%H:%M"),
                        "duration": dur_str,
                        "start_dt": target_start,
                        "end_dt": target_end,
                        "attendees": people,
                        "location": loc_str
                    }

                    # Para la ventana próxima de 8h
                    if target_end >= now_ba and target_start <= window_end_ba:
                        events_window.append(event_dict)

                    # Para el timeline de 8 a 17h
                    if target_end >= day_start_ba and target_start <= day_end_ba:
                        events_today_daytime.append(event_dict)

        events_window.sort(key=lambda x: x["start"])
        events_today_daytime.sort(key=lambda x: x["start"])
        with CACHE_LOCK:
            CALENDAR_CACHE["events"] = events_window
            CALENDAR_CACHE["today_all_events"] = events_today_daytime
            CALENDAR_CACHE["timestamp"] = time.time()
        print(f"[BG WORKER] Calendario actualizado: {len(events_window)} citas ventana, {len(events_today_daytime)} citas timeline 8-17h")
    except Exception as e:
        print(f"[BG WORKER] Error en calendario: {e}")

def update_weather_data_sync():
    try:
        url = 'https://api.open-meteo.com/v1/forecast?latitude=-34.6037&longitude=-58.3816&current=temperature_2m,weather_code&daily=weather_code,temperature_2m_max,temperature_2m_min&timezone=America%2FArgentina%2FBuenos_Aires'
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode())
            with CACHE_LOCK:
                WEATHER_CACHE["data"] = data
                WEATHER_CACHE["timestamp"] = time.time()
            print("[BG WORKER] Clima actualizado")
    except Exception as e:
        print(f"[BG WORKER] Error en clima: {e}")

def render_png_dashboard():
    """Genera la imagen PNG exacta de 800x480 píxeles usando Pillow y la guarda en RAM"""
    try:
        width, height = 800, 480
        img = Image.new('RGB', (width, height), color='#FFFFFF')
        draw = ImageDraw.Draw(img)

        # Tipografías optimizadas
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

        temp_cur = "14°"
        desc_cur = "Mayormente despejado"
        range_cur = "Mín: 12° | Máx: 17°"
        sat_temp = "8°/13°"
        sun_temp = "4°/11°"
        sat_code = 2
        sun_code = 0
        cur_code = 1

        if wdata:
            try:
                t = round(wdata["current"]["temperature_2m"])
                temp_cur = f"{t}°"
                code = wdata["current"]["weather_code"]
                cur_code = code
                if code == 0: desc_cur = "Despejado"
                elif code in (1, 2): desc_cur = "Mayormente despejado"
                elif code == 3: desc_cur = "Nublado"
                elif 51 <= code <= 65: desc_cur = "Lluvia ligera"
                elif 80 <= code <= 82: desc_cur = "Chaparrones"
                elif code >= 95: desc_cur = "Tormenta"
                
                min_c = round(wdata["daily"]["temperature_2m_min"][0])
                max_c = round(wdata["daily"]["temperature_2m_max"][0])
                range_cur = f"Mín: {min_c}° | Máx: {max_c}°"

                times = wdata["daily"]["time"]
                for i, ts in enumerate(times):
                    d = datetime.strptime(ts, "%Y-%m-%d")
                    if d.weekday() == 5:
                        sat_temp = f"{round(wdata['daily']['temperature_2m_min'][i])}°/{round(wdata['daily']['temperature_2m_max'][i])}°"
                        sat_code = wdata["daily"]["weather_code"][i]
                    if d.weekday() == 6:
                        sun_temp = f"{round(wdata['daily']['temperature_2m_min'][i])}°/{round(wdata['daily']['temperature_2m_max'][i])}°"
                        sun_code = wdata["daily"]["weather_code"][i]
            except Exception:
                pass

        # 1. CABECERA (Ajuste aprobado: sin BUENOS AIRES a la derecha)
        draw.rounded_rectangle([8, 8, 792, 74], radius=6, outline="#000000", width=2, fill="#FFFFFF")
        draw.text((20, 24), time_str, font=font_clock, fill="#0044CC")
        draw.line([104, 16, 104, 66], fill="#000000", width=2)
        draw.text((114, 24), day_str, font=font_day, fill="#000000")
        draw.text((114, 46), "Buenos Aires", font=font_meta, fill="#000000")

        draw.line([325, 16, 325, 66], fill="#000000", width=2)

        # Pronóstico Fin de Semana
        draw.text((335, 20), "PRONÓSTICO FIN DE SEMANA", font=font_meta, fill="#0044CC")
        draw_weather_icon(draw, sat_code, 345, 48, r=6)
        draw.text((358, 42), f"SÁB: {sat_temp}", font=font_label, fill="#000000")
        draw_weather_icon(draw, sun_code, 440, 48, r=6)
        draw.text((453, 42), f"DOM: {sun_temp}", font=font_label, fill="#000000")

        draw.line([525, 16, 525, 66], fill="#000000", width=2)

        # Clima actual ampliado
        draw_weather_icon(draw, cur_code, 550, 41, r=10)
        draw.text((572, 24), temp_cur, font=font_clock, fill="#000000")
        draw.text((634, 27), desc_cur, font=font_desc_clima, fill="#000000")
        draw.text((634, 48), range_cur, font=font_range_clima, fill="#0044CC")

        # 2. PANEL AGENDA
        draw.rounded_rectangle([8, 80, 448, 472], radius=6, outline="#000000", width=2, fill="#FFFFFF")
        draw.text((20, 92), "PRÓXIMAS 8 HORAS", font=font_title, fill="#000000")
        draw.rounded_rectangle([325, 88, 436, 108], radius=3, fill="#0044CC")
        draw.text((332, 92), window_str, font=font_meta, fill="#FFFFFF")
        draw.line([10, 114, 446, 114], fill="#000000", width=2)

        # =========================================================================
        # TIMELINE DE 8 A 17 HS: ALTURA 8px · NEGRO (OCUPADO) / BLANCO (LIBRE)
        # =========================================================================
        tl_x = 16
        tl_y = 124
        tl_w = 424
        tl_h = 8 # ALTURA 8px (la mitad)

        # Base BLANCA (#FFFFFF) = Tiempo libre / disponible
        draw.rounded_rectangle([tl_x, tl_y, tl_x + tl_w, tl_y + tl_h], radius=2, fill="#FFFFFF", outline="#000000", width=1)

        def time_to_tl_x(hh, mm):
            mins = (hh - 8) * 60 + mm
            ratio = max(0.0, min(1.0, mins / 540.0))
            return int(tl_x + ratio * tl_w)

        # Zonas ocupadas en NEGRO SÓLIDO (#000000) calculadas a partir de las citas del día
        for ev in timeline_events:
            s_dt = ev.get("start_dt")
            e_dt = ev.get("end_dt")
            if s_dt and e_dt:
                x_start = time_to_tl_x(s_dt.hour, s_dt.minute)
                x_end = time_to_tl_x(e_dt.hour, e_dt.minute)
                if x_end > x_start:
                    draw.rectangle([x_start, tl_y + 1, x_end, tl_y + tl_h - 1], fill="#000000")

        # Marcas cada 1 hora (8 a 17 hs) con etiquetas cada 3 horas (08h, 11h, 14h, 17h)
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
                if h == 8:
                    tx = hx
                elif h == 17:
                    tx = hx - hw
                else:
                    tx = hx - hw // 2
                draw.text((tx, tl_y + tl_h + 5), h_str, font=font_tick, fill="#000000")

        # Marcador de hora actual ("AHORA") en ROJO (#D60000)
        cur_hour = now_ba.hour
        cur_min = now_ba.minute
        if 8 <= cur_hour <= 17:
            now_x = time_to_tl_x(cur_hour, cur_min)
            draw.line([now_x, tl_y - 4, now_x, tl_y + tl_h + 2], fill="#D60000", width=2)
            draw.polygon([(now_x - 3, tl_y - 5), (now_x + 3, tl_y - 5), (now_x, tl_y - 1)], fill="#D60000")

        # Línea divisoria bajo el timeline
        draw.line([12, tl_y + 28, 444, tl_y + 28], fill="#E5E7EB", width=1)

        # =========================================================================
        # REUNIONES FORMATO "PROPUESTA A" (Horario y Título al mismo nivel)
        # =========================================================================
        if not events:
            draw.rounded_rectangle([20, tl_y + 44, 436, 455], radius=4, outline="#000000", width=1, fill="#FFFFFF")
            draw.text((120, tl_y + 110), "Sin citas en las próximas 8 horas", font=font_title, fill="#008833")
            draw.text((95, tl_y + 135), "Tu calendario no registra compromisos en este período", font=font_label, fill="#000000")
        else:
            y_card = tl_y + 35
            card_h = 51
            gap = 6
            
            for evt in events[:6]:
                draw.rounded_rectangle([16, y_card, 440, y_card + card_h], radius=4, outline="#000000", width=1, fill="#FFFFFF")
                draw.rectangle([16, y_card, 21, y_card + card_h], fill="#0044CC")
                
                s_val = str(evt.get('start') or '--:--')
                e_val = str(evt.get('end') or '--:--')
                t_full = f"{s_val} – {e_val}"
                dur = str(evt.get("duration") or "30m")
                t_str = str(evt.get("title") or "Reunión")[:25]

                # Fila 1: Horario + Título grande en negrita (13px) al mismo nivel
                draw.text((28, y_card + 5), t_full, font=font_time, fill="#0044CC")
                draw.text((120, y_card + 4), "•", font=font_time, fill="#888888")
                draw.text((130, y_card + 4), t_str, font=font_t_big, fill="#000000")
                
                # Pastilla de duración a la derecha
                bbox_d = draw.textbbox((0, 0), dur, font=font_label)
                dw = int(bbox_d[2] - bbox_d[0])
                pw = max(dw + 8, 30)
                draw.rounded_rectangle([432 - pw, y_card + 5, 432, y_card + 20], radius=2, fill="#000000")
                draw.text((432 - pw + 4, y_card + 6), dur, font=font_label, fill="#FFFFFF")
                
                # Fila 2: Detalles (Ubicación / Asistente) en negro sólido
                sub = str(evt.get("location") or ("🍽️ Almuerzo" if "almuerzo" in t_str.lower() else "📍 Microsoft Teams"))
                if evt.get("attendees"):
                    sub = f"👤 {', '.join(str(a) for a in evt['attendees'])}"
                draw.text((28, y_card + 29), sub[:48], font=font_label, fill="#000000")
                
                y_card += card_h + gap

        # 3. PANEL FINANZAS
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
                
                chart_x = x_c + 7
                chart_y = y_c + 48
                chart_w = 144
                chart_h = 36
                draw.rounded_rectangle([chart_x, chart_y, chart_x + chart_w, chart_y + chart_h], radius=3, fill="#FAFAFA", outline="#E5E7EB", width=1)
                draw.text((chart_x + 3, chart_y + 2), "60D", font=font_label, fill="#9CA3AF")
                
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
    try:
        update_finance_data_sync()
        update_weather_data_sync()
        update_calendar_data_sync()
        render_png_dashboard()
        print("[BG WORKER] Primera imagen generada con éxito con todos los datos.")
    except Exception as e:
        print(f"[BG WORKER] Error inicial: {e}")

    while True:
        time.sleep(300)
        try:
            update_finance_data_sync()
            update_weather_data_sync()
            update_calendar_data_sync()
            render_png_dashboard()
            print(f"[BG WORKER] Imagen actualizada ({datetime.now().strftime('%H:%M:%S')})")
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

            elif self.path.startswith("/api/debug_calendar"):
                with CACHE_LOCK:
                    debug_copy = dict(CALENDAR_CACHE.get("debug", {}))
                    debug_copy["events"] = CALENDAR_CACHE.get("events", [])
                body = json.dumps(debug_copy, indent=2).encode("utf-8")
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
        except Exception as err:
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
