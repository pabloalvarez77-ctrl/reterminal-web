#!/usr/bin/env python3
"""
Servidor para reTerminal E1002 con Server-Side Rendering (SSR)
- Velas japonesas de 60 días para cada activo (Verde #008833, Rojo #D60000 nativos de Spectra 6)
- Variación porcentual DIARIA calculada respecto al cierre de la sesión anterior (ayer)
- Íconos de clima de alto contraste (contorno negro nítido de 2px con relleno amarillo puro)
- Filtrado estricto de reuniones canceladas y visualización estética de agenda
- Entrega HTML 100% completo al primer milisegundo
"""

import http.server
import socketserver
import json
import urllib.request
import re
import os
import time
import gzip
from datetime import datetime, timedelta, timezone

PORT = int(os.environ.get("PORT", 5000))

# URL directa oficial de Outlook / Office 365 (Bitali)
ICAL_URL = os.environ.get(
    "ICAL_URL",
    "https://outlook.office365.com/owa/calendar/4ebd49ac2ad843ee9cc8519536437d40@bitali.com/dc1dbade01494a418a37e61fda7f3e7b6471521803256032760/calendar.ics"
)

# Google Sheet 'Activos' de Pablo
SHEET_ID = "1t1l4MjlXuid0ljh2zZuUC-5mAyHVZyQUrjV-NtfXKx4"
SHEET_CSV_URL = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/export?format=csv"

# Títulos de reuniones canceladas/eliminadas para exclusión inmediata
EXCLUDED_TITLES = ["proyecto 90k", "graciela maestra pedro", "cancelado", "canceled", "rechazado"]

# Cachés en memoria
CALENDAR_CACHE = {"events": [], "debug": {}, "timestamp": 0, "ttl": 300}
FINANCE_CACHE = {"data": [], "timestamp": 0, "ttl": 300}
WEATHER_CACHE = {"data": None, "timestamp": 0, "ttl": 900}

def get_tickers_from_sheet():
    try:
        req = urllib.request.Request(SHEET_CSV_URL, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=6) as resp:
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
        print(f"[FINANCE] Error al leer Google Sheet: {e}")
        return []

def generate_candles_svg(candles, width=144, height=36):
    """
    Genera un SVG con 60 velas japonesas (OHLC)
    Verde primario: #008833
    Rojo primario: #D60000
    """
    if not candles or len(candles) < 2:
        return f'<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}"><rect width="{width}" height="{height}" fill="#FAFAFA" rx="3"/></svg>'
    
    all_lows = [c[2] for c in candles if c[2] is not None and c[2] > 0]
    all_highs = [c[3] for c in candles if c[3] is not None and c[3] > 0]
    if not all_lows or not all_highs:
        return f'<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}"><rect width="{width}" height="{height}" fill="#FAFAFA" rx="3"/></svg>'
        
    p_min = min(all_lows)
    p_max = max(all_highs)
    p_range = p_max - p_min if p_max > p_min else 1.0
    
    n = len(candles)
    pad_x = 3.0
    usable_w = width - 2 * pad_x
    step = usable_w / max(n - 1, 1)
    
    pad_y = 2.5
    usable_h = height - 2 * pad_y
    
    elements = []
    elements.append(f'<rect x="0" y="0" width="{width}" height="{height}" fill="#FAFAFA" rx="3" stroke="#E5E7EB" stroke-width="0.8"/>')
    elements.append(f'<text x="4" y="9" font-size="6.5" font-weight="800" fill="#9CA3AF" font-family="sans-serif">60D</text>')
    
    for i, (op, hi, lo, cl) in enumerate(candles):
        if None in (op, hi, lo, cl) or op <= 0 or cl <= 0:
            continue
            
        is_up = cl >= op
        col = "#008833" if is_up else "#D60000"
        
        cx = pad_x + i * step
        
        y_hi = height - (pad_y + (hi - p_min) / p_range * usable_h)
        y_lo = height - (pad_y + (lo - p_min) / p_range * usable_h)
        y_op = height - (pad_y + (op - p_min) / p_range * usable_h)
        y_cl = height - (pad_y + (cl - p_min) / p_range * usable_h)
        
        elements.append(f'<line x1="{cx:.1f}" y1="{y_hi:.1f}" x2="{cx:.1f}" y2="{y_lo:.1f}" stroke="{col}" stroke-width="0.9"/>')
        
        body_top = min(y_op, y_cl)
        body_h = max(abs(y_cl - y_op), 1.2)
        body_w = max(step * 0.75, 1.2)
        bx = cx - body_w / 2.0
        
        elements.append(f'<rect x="{bx:.1f}" y="{body_top:.1f}" width="{body_w:.1f}" height="{body_h:.1f}" fill="{col}" stroke="{col}" stroke-width="0.3"/>')
        
    return f'<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}" xmlns="http://www.w3.org/2000/svg">{"".join(elements)}</svg>'

def fetch_finance_data():
    now_ts = time.time()
    if FINANCE_CACHE["data"] and (now_ts - FINANCE_CACHE["timestamp"]) < FINANCE_CACHE["ttl"]:
        return FINANCE_CACHE["data"]

    tickers = get_tickers_from_sheet()
    if not tickers:
        return []

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
                short_name = meta.get("shortName", meta.get("symbol", label))
                
                # Extraer velas históricas de 60 días
                quote = result.get("indicators", {}).get("quote", [{}])[0]
                opens = quote.get("open", [])
                highs = quote.get("high", [])
                lows = quote.get("low", [])
                closes = quote.get("close", [])
                
                valid_candles = []
                for o, h, l, c in zip(opens, highs, lows, closes):
                    if None not in (o, h, l, c) and o > 0 and h > 0 and l > 0 and c > 0:
                        valid_candles.append((o, h, l, c))
                
                # VARIACIÓN DIARIA RESPECTO AL CIERRE DE LA RUEDA ANTERIOR (AYER)
                # En Yahoo Finance con range=3mo, chartPreviousClose es de hace 3 meses.
                # Para la variación diaria se usa regularMarketPreviousClose o el cierre de la anteúltima vela.
                prev_close = meta.get("regularMarketPreviousClose")
                if not prev_close or prev_close <= 0:
                    if len(valid_candles) >= 2:
                        prev_close = valid_candles[-2][3]  # Cierre de ayer
                    else:
                        prev_close = price
                
                if prev_close and prev_close > 0 and prev_close != price:
                    change_pct = ((price - prev_close) / prev_close) * 100
                else:
                    change_pct = 0.0
                
                up = change_pct >= 0
                sign = "+" if up else ""
                price_str = f"{price:,.2f}" if price >= 1000 else f"{price:.2f}"
                
                candles_60d = valid_candles[-60:]
                chart_svg = generate_candles_svg(candles_60d, width=144, height=36)
                
                results.append({
                    "sym": label, "name": short_name, "price": price_str,
                    "change": f"{sign}{change_pct:.2f}%", "up": up,
                    "chart_svg": chart_svg
                })
        except Exception as err:
            print(f"[FINANCE] Error con {sym}: {err}")
            results.append({
                "sym": label, "name": label, "price": "N/A", "change": "0.00%", "up": True,
                "chart_svg": ""
            })

    FINANCE_CACHE["data"] = results
    FINANCE_CACHE["timestamp"] = now_ts
    return results

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
    lower = name.lower().strip()
    bad_tokens = (
        'thread.', '19_meeting', '19:', 'resource.calendar', 'skype',
        'microsoft teams', 'reunión de microsoft', 'teams meeting'
    )
    if any(x in lower for x in bad_tokens):
        return False
    return len(name.strip()) >= 2

def extract_people_from_vevent(raw):
    people = []

    # 1. Extraer ORGANIZER
    org_line = re.search(r'ORGANIZER[^\r\n]+', raw, re.IGNORECASE)
    if org_line:
        line = org_line.group(0)
        cn = re.search(r';CN=(?:"([^"]+)"|([^;:\r\n]+))', line, re.IGNORECASE)
        if cn:
            cand = (cn.group(1) or cn.group(2)).strip().replace('"', '')
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

    # 2. Extraer ATTENDEE
    for line in re.findall(r'ATTENDEE[^\r\n]+', raw, re.IGNORECASE):
        cn = re.search(r';CN=(?:"([^"]+)"|([^;:\r\n]+))', line, re.IGNORECASE)
        if cn:
            cand = (cn.group(1) or cn.group(2)).strip().replace('"', '')
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

    # 3. Extraer X-MS-OLK-SENDER
    sender_line = re.search(r'X-MS-OLK-SENDER[^\r\n]+', raw, re.IGNORECASE)
    if sender_line:
        line = sender_line.group(0)
        cn = re.search(r';CN=(?:"([^"]+)"|([^;:\r\n]+))', line, re.IGNORECASE)
        if cn:
            cand = (cn.group(1) or cn.group(2)).strip().replace('"', '')
            if is_clean_human_name(cand) and cand not in people:
                people.append(cand)

    return people

def get_calendar_data_and_debug(force_refresh=False):
    global CALENDAR_CACHE
    now_ts = time.time()

    if not force_refresh and CALENDAR_CACHE["events"] and (now_ts - CALENDAR_CACHE["timestamp"]) < CALENDAR_CACHE["ttl"]:
        debug_copy = dict(CALENDAR_CACHE["debug"])
        debug_copy["from_cache"] = True
        return CALENDAR_CACHE["events"], debug_copy

    tz_ba = timezone(timedelta(hours=-3))
    now_ba = datetime.now(tz_ba)
    window_end_ba = now_ba + timedelta(hours=8)
    
    debug_info = {
        "ical_url": ICAL_URL,
        "now_ba": now_ba.strftime("%Y-%m-%d %H:%M:%S"),
        "window_end_ba": window_end_ba.strftime("%Y-%m-%d %H:%M:%S"),
        "http_status": None,
        "content_length": 0,
        "total_vevents": 0,
        "cancelled_filtered": 0,
        "matched_events": 0,
        "from_cache": False,
        "error": None
    }

    events = []

    try:
        req = urllib.request.Request(
            ICAL_URL,
            headers={
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
                'Accept': 'text/calendar, text/plain, */*',
                'Accept-Encoding': 'gzip, deflate',
                'Connection': 'keep-alive'
            }
        )
        with urllib.request.urlopen(req, timeout=35) as resp:
            debug_info["http_status"] = resp.status
            raw_data = resp.read()
            encoding = resp.headers.get("Content-Encoding", "").lower()
            if "gzip" in encoding or raw_data.startswith(b'\x1f\x8b'):
                content = gzip.decompress(raw_data).decode('utf-8', errors='ignore')
            else:
                content = raw_data.decode('utf-8', errors='ignore')
            debug_info["content_length"] = len(content)

        unfolded = re.sub(r'\r?\n[ \t]', '', content)
        raw_events = re.findall(r'BEGIN:VEVENT(.*?)END:VEVENT', unfolded, re.DOTALL | re.IGNORECASE)
        debug_info["total_vevents"] = len(raw_events)

        weekday_map = {0: "MO", 1: "TU", 2: "WE", 3: "TH", 4: "FR", 5: "SA", 6: "SU"}
        today_code = weekday_map[now_ba.weekday()]

        for raw in raw_events:
            # 1. FILTRAR CANCELADAS O LIBRES
            status_m = re.search(r'STATUS(?:;[^:\r\n]*)?:\s*([A-Z]+)', raw, re.IGNORECASE)
            status = status_m.group(1).upper() if status_m else ""
            
            busy_m = re.search(r'X-MICROSOFT-CDO-BUSYSTATUS:\s*([A-Z]+)', raw, re.IGNORECASE)
            busy_status = busy_m.group(1).upper() if busy_m else ""

            if status == "CANCELLED" or busy_status == "FREE":
                debug_info["cancelled_filtered"] += 1
                continue

            summary_m = re.search(r'SUMMARY(?:;[^:\r\n]*)?:(.*?)\r?\n', raw, re.IGNORECASE)
            summary = summary_m.group(1).strip() if summary_m else "Reunión programada"
            summary = summary.replace('\\,', ',').replace('\\;', ';')

            if any(ex in summary.lower() for ex in EXCLUDED_TITLES):
                debug_info["cancelled_filtered"] += 1
                continue

            # 2. EXTRAER PERSONAS REALES
            people = extract_people_from_vevent(raw)

            # 3. EXTRAER UBICACIÓN
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

                # Caso A: Evento fechado hoy
                if dt_end >= now_ba and dt_start <= window_end_ba:
                    target_start = dt_start
                    target_end = dt_end
                # Caso B: Evento recurrente
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
                        if cand_end >= now_ba and cand_start <= window_end_ba:
                            target_start = cand_start
                            target_end = cand_end

                if target_start and target_end:
                    dur_min = int((target_end - target_start).total_seconds() / 60)
                    dur_str = f"{dur_min}m" if dur_min < 60 else f"{dur_min//60}h"
                    
                    events.append({
                        "title": summary,
                        "start": target_start.strftime("%H:%M"),
                        "end": target_end.strftime("%H:%M"),
                        "duration": dur_str,
                        "attendees": people,
                        "location": loc_str
                    })

        events.sort(key=lambda x: x["start"])
        debug_info["matched_events"] = len(events)
        CALENDAR_CACHE["events"] = events
        CALENDAR_CACHE["debug"] = debug_info
        CALENDAR_CACHE["timestamp"] = time.time()
        print(f"[CALENDAR] {len(events)} citas activas encontradas ({debug_info['cancelled_filtered']} canceladas/excluidas)")
    except Exception as e:
        debug_info["error"] = str(e)
        print(f"[CALENDAR] Error: {e}")
        if CALENDAR_CACHE["events"]:
            return CALENDAR_CACHE["events"], debug_info

    return events, debug_info

def fetch_weather_server():
    now_ts = time.time()
    if WEATHER_CACHE["data"] and (now_ts - WEATHER_CACHE["timestamp"]) < WEATHER_CACHE["ttl"]:
        return WEATHER_CACHE["data"]
    try:
        url = 'https://api.open-meteo.com/v1/forecast?latitude=-34.6037&longitude=-58.3816&current=temperature_2m,weather_code&daily=weather_code,temperature_2m_max,temperature_2m_min&timezone=America%2FArgentina%2FBuenos_Aires'
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode())
            WEATHER_CACHE["data"] = data
            WEATHER_CACHE["timestamp"] = now_ts
            return data
    except Exception:
        return None

def get_high_contrast_weather_svg(code, size=22):
    """Genera íconos de clima con borde negro nítido de 2px y relleno amarillo/azul puro"""
    if code in (0, 1):
        return f'''<svg width="{size}" height="{size}" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
            <circle cx="12" cy="12" r="5" fill="#FFCC00" stroke="#000000" stroke-width="2"/>
            <path d="M12 1v3M12 20v3M1 12h3M20 12h3M4.22 4.22l2.12 2.12M17.66 17.66l2.12 2.12M4.22 19.78l2.12-2.12M17.66 6.34l2.12-2.12" stroke="#000000" stroke-width="2" stroke-linecap="round"/>
        </svg>'''
    elif code in (2, 3):
        return f'''<svg width="{size}" height="{size}" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
            <path d="M17 17a5 5 0 0 0-10 0" fill="#FFCC00" stroke="#000000" stroke-width="1.5"/>
            <path d="M17.5 19H9a6 6 0 1 1 5.9-4.8A4.5 4.5 0 1 1 17.5 19z" fill="#FFFFFF" stroke="#000000" stroke-width="2" stroke-linejoin="round"/>
        </svg>'''
    else:
        return f'''<svg width="{size}" height="{size}" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
            <path d="M17.5 15H9a6 6 0 1 1 5.9-4.8A4.5 4.5 0 1 1 17.5 15z" fill="#FFFFFF" stroke="#000000" stroke-width="2"/>
            <line x1="8" y1="18" x2="8" y2="21" stroke="#0044CC" stroke-width="2.5" stroke-linecap="round"/>
            <line x1="12" y1="18" x2="12" y2="21" stroke="#0044CC" stroke-width="2.5" stroke-linecap="round"/>
            <line x1="16" y1="18" x2="16" y2="21" stroke="#0044CC" stroke-width="2.5" stroke-linecap="round"/>
        </svg>'''

def build_ssr_html(template_content):
    tz_ba = timezone(timedelta(hours=-3))
    now_ba = datetime.now(tz_ba)
    window_end_ba = now_ba + timedelta(hours=8)
    
    pad = lambda n: str(n).zfill(2)
    time_str = f"{pad(now_ba.hour)}:{pad(now_ba.minute)}"
    window_str = f"{time_str} – {pad(window_end_ba.hour)}:{pad(window_end_ba.minute)}"
    
    dias = ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo"]
    meses = ["Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio", "Julio", "Agosto", "Septiembre", "Octubre", "Noviembre", "Diciembre"]
    day_str = f"{dias[now_ba.weekday()]}, {now_ba.day} de {meses[now_ba.month - 1]}"

    # Clima
    wdata = fetch_weather_server()
    temp_cur = "14°"
    desc_cur = "Mayormente despejado"
    range_cur = "Mín: 12° | Máx: 17°"
    sat_temp = "8°/13°"
    sun_temp = "4°/11°"
    weather_icon_html = get_high_contrast_weather_svg(1, 26)
    sat_icon_html = get_high_contrast_weather_svg(2, 18)
    sun_icon_html = get_high_contrast_weather_svg(0, 18)

    if wdata:
        try:
            t = round(wdata["current"]["temperature_2m"])
            temp_cur = f"{t}°"
            code = wdata["current"]["weather_code"]
            if code == 0: desc_cur = "Despejado"
            elif code in (1, 2): desc_cur = "Mayormente despejado"
            elif code == 3: desc_cur = "Nublado"
            elif 51 <= code <= 65: desc_cur = "Lluvia ligera"
            elif 80 <= code <= 82: desc_cur = "Chaparrones"
            elif code >= 95: desc_cur = "Tormenta"
            
            weather_icon_html = get_high_contrast_weather_svg(code, 26)

            min_c = round(wdata["daily"]["temperature_2m_min"][0])
            max_c = round(wdata["daily"]["temperature_2m_max"][0])
            range_cur = f"Mín: {min_c}° | Máx: {max_c}°"

            times = wdata["daily"]["time"]
            for i, ts in enumerate(times):
                d = datetime.strptime(ts, "%Y-%m-%d")
                if d.weekday() == 5:
                    sat_temp = f"{round(wdata['daily']['temperature_2m_min'][i])}°/{round(wdata['daily']['temperature_2m_max'][i])}°"
                    sat_icon_html = get_high_contrast_weather_svg(wdata["daily"]["weather_code"][i], 18)
                if d.weekday() == 6:
                    sun_temp = f"{round(wdata['daily']['temperature_2m_min'][i])}°/{round(wdata['daily']['temperature_2m_max'][i])}°"
                    sun_icon_html = get_high_contrast_weather_svg(wdata["daily"]["weather_code"][i], 18)
        except Exception:
            pass

    # Citas de Calendario
    events, _ = get_calendar_data_and_debug()
    if not events:
        events_html = """
          <div class="empty-state">
            <svg class="icon icon-lg" viewBox="0 0 24 24" style="stroke: #008833;"><path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"></path><polyline points="22 4 12 14.01 9 11.01"></polyline></svg>
            <div class="empty-state-title">Sin citas en las próximas 8 horas</div>
            <div class="empty-state-sub">Tu calendario no registra compromisos en este período</div>
          </div>"""
    else:
        is_roomy = len(events) <= 4
        cards = []
        for evt in events[:6]:
            roomy_cls = " roomy" if is_roomy else ""
            
            if evt.get("attendees"):
                sub_text = f"""<svg class="icon" viewBox="0 0 24 24" style="stroke: #000000; min-width: 12px; width:12px; height:12px;"><path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"></path><circle cx="9" cy="7" r="4"></circle></svg><span>{", ".join(evt["attendees"])}</span>"""
            elif evt.get("location"):
                sub_text = f"""<svg class="icon" viewBox="0 0 24 24" style="stroke: #000000; min-width: 12px; width:12px; height:12px;"><path d="M21 10c0 7-9 13-9 13s-9-6-9-13a9 9 0 0 1 18 0z"></path><circle cx="12" cy="10" r="3"></circle></svg><span>{evt["location"]}</span>"""
            elif "almuerzo" in evt.get("title", "").lower():
                sub_text = """<span>🍽️ Almuerzo</span>"""
            else:
                sub_text = """<span>📅 Reunión agendada</span>"""

            card = f"""
            <div class="event-card{roomy_cls}">
              <div class="event-top">
                <div class="event-time">
                  <svg class="icon" viewBox="0 0 24 24" style="stroke: #0044CC; width:12px; height:12px;"><circle cx="12" cy="12" r="9"></circle><polyline points="12 6 12 12 16 14"></polyline></svg>
                  <span>{evt['start']} – {evt['end']}</span>
                </div>
                <span class="event-duration">{evt.get('duration', '30m')}</span>
              </div>
              <div class="event-title">{evt['title']}</div>
              <div class="event-subrow">{sub_text}</div>
            </div>"""
            cards.append(card)
        events_html = "\n".join(cards)

    # Finanzas con velas y variación diaria calculada
    stocks = fetch_finance_data()
    if not stocks:
        stocks_html = """
          <div class="empty-state" style="grid-column: span 2;">
            <svg class="icon icon-lg" viewBox="0 0 24 24" style="stroke: #000000; width: 22px; height: 22px;">
              <circle cx="12" cy="12" r="10"></circle>
              <line x1="12" y1="8" x2="12" y2="12"></line>
              <line x1="12" y1="16" x2="12.01" y2="16"></line>
            </svg>
            <div class="empty-state-title">Sin información disponible</div>
            <div class="empty-state-sub">No se pudieron obtener las cotizaciones de tu cartera</div>
          </div>"""
    else:
        cards = []
        for st in stocks:
            is_up = st.get("up", True)
            badge_cls = "badge-up" if is_up else "badge-down"
            arrow = """<svg class="icon" style="width:11px;height:11px;stroke:#FFFFFF;" viewBox="0 0 24 24"><polyline points="18 15 12 9 6 15"></polyline></svg>""" if is_up else """<svg class="icon" style="width:11px;height:11px;stroke:#FFFFFF;" viewBox="0 0 24 24"><polyline points="6 9 12 15 18 9"></polyline></svg>"""
            chart_svg = st.get("chart_svg", "")
            
            card = f"""
            <div class="stock-card">
              <div class="stock-card-top">
                <span class="stock-sym">{st['sym']}</span>
                <span class="badge-pill {badge_cls}">{arrow} {st['change']}</span>
              </div>
              <div class="stock-val">${st['price']}</div>
              <div class="stock-chart">{chart_svg}</div>
              <div class="stock-desc">{st['name']}</div>
            </div>"""
            cards.append(card)
        stocks_html = "\n".join(cards)

    # Inyección directa de Server-Side Rendering
    rendered = template_content
    rendered = rendered.replace('<span id="header-time">--:--</span>', f'<span id="header-time">{time_str}</span>')
    rendered = rendered.replace('<div class="date-day" id="header-day">--</div>', f'<div class="date-day" id="header-day">{day_str}</div>')
    rendered = rendered.replace('<span id="window-range">--:-- – --:--</span>', f'<span id="window-range">{window_str}</span>')
    
    rendered = rendered.replace('<div class="weather-temp" id="weather-temp">--°</div>', f'<div class="weather-temp" id="weather-temp">{temp_cur}</div>')
    rendered = rendered.replace('<div id="weather-desc">Cargando...</div>', f'<div id="weather-desc">{desc_cur}</div>')
    rendered = rendered.replace('<div id="weather-range">Mín: --° | Máx: --°</div>', f'<div id="weather-range">{range_cur}</div>')
    rendered = rendered.replace('<span id="sat-temp">--°/--°</span>', f'<span id="sat-temp">{sat_temp}</span>')
    rendered = rendered.replace('<span id="sun-temp">--°/--°</span>', f'<span id="sun-temp">{sun_temp}</span>')
    
    rendered = rendered.replace('<div class="weather-icon-box" id="weather-icon-box">', f'<div class="weather-icon-box" id="weather-icon-box">{weather_icon_html}<!--')
    rendered = rendered.replace('</div>\n      <div class="weather-temp"', f'--></div>\n      <div class="weather-temp"')
    rendered = rendered.replace('<span id="sat-icon"></span>', f'<span id="sat-icon">{sat_icon_html}</span>')
    rendered = rendered.replace('<span id="sun-icon"></span>', f'<span id="sun-icon">{sun_icon_html}</span>')

    rendered = rendered.replace('<div class="events-container" id="events-container"></div>', f'<div class="events-container" id="events-container">{events_html}</div>')
    rendered = rendered.replace('<div class="stocks-grid" id="stocks-grid"></div>', f'<div class="stocks-grid" id="stocks-grid">{stocks_html}</div>')

    return rendered

class RequestHandler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/api/finance":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps(fetch_finance_data()).encode())
            return
        elif self.path == "/api/calendar":
            events, _ = get_calendar_data_and_debug()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps(events).encode())
            return
        elif self.path.startswith("/api/debug_calendar"):
            force = "refresh=true" in self.path
            events, debug_info = get_calendar_data_and_debug(force_refresh=force)
            debug_info["events"] = events
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps(debug_info, indent=2).encode())
            return
        elif self.path in ("/", "/index.html"):
            template_path = "index.html" if os.path.exists("index.html") else "dashboard_perfect.html"
            with open(template_path, "r", encoding="utf-8") as f:
                template_str = f.read()

            final_html = build_ssr_html(template_str)
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            self.end_headers()
            self.wfile.write(final_html.encode("utf-8"))
            return

        return super().do_GET()

if __name__ == "__main__":
    print(f"Servidor activo en el puerto {PORT} con variación diaria corregida")
    with socketserver.TCPServer(("", PORT), RequestHandler) as httpd:
        httpd.serve_forever()
