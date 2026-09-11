#!/usr/bin/env python3
"""
Servidor para reTerminal E1002 con Generador Automático de Imagen Estática (dashboard.png)
- Proceso en segundo plano cada 15 minutos: descarga datos y dibuja la imagen exacta de 800x480 px con Pillow
- Sirve /dashboard.png desde la memoria RAM en <2 milisegundos (Cero timeouts, cero "Failed to load page")
- Mantiene APIs /api/calendar, /api/finance y vista web en /
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
CALENDAR_CACHE = {"events": [], "debug": {}, "timestamp": 0}
FINANCE_CACHE = {"data": [], "timestamp": 0}
WEATHER_CACHE = {"data": None, "timestamp": 0}
IMAGE_CACHE = {"bytes": None, "timestamp": 0}

def get_font(size):
    font_paths = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
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

def get_tickers_from_sheet():
    try:
        req = urllib.request.Request(SHEET_CSV_URL, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=5) as resp:
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
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read().decode())
                result = data["chart"]["result"][0]
                meta = result["meta"]
                
                price = meta.get("regularMarketPrice", 0)
                short_name = meta.get("shortName", meta.get("symbol", label))
                
                quote = result.get("indicators", {}).get("quote", [{}])[0]
                opens = quote.get("open", [])
                highs = quote.get("high", [])
                lows = quote.get("low", [])
                closes = quote.get("close", [])
                
                valid_candles = []
                for o, h, l, c in zip(opens, highs, lows, closes):
                    if None not in (o, h, l, c) and o > 0 and h > 0 and l > 0 and c > 0:
                        valid_candles.append((o, h, l, c))
                
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

    return people

def update_calendar_data_sync():
    tz_ba = timezone(timedelta(hours=-3))
    now_ba = datetime.now(tz_ba)
    window_end_ba = now_ba + timedelta(hours=8)
    events = []

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

            if status == "CANCELLED" or busy_status == "FREE":
                continue

            summary_m = re.search(r'SUMMARY(?:;[^:\r\n]*)?:(.*?)\r?\n', raw, re.IGNORECASE)
            summary = summary_m.group(1).strip() if summary_m else "Reunión programada"
            summary = summary.replace('\\,', ',').replace('\\;', ';')

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

                if dt_end >= now_ba and dt_start <= window_end_ba:
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
        with CACHE_LOCK:
            CALENDAR_CACHE["events"] = events
            CALENDAR_CACHE["timestamp"] = time.time()
        print(f"[BG WORKER] Calendario actualizado: {len(events)} citas")
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
    width, height = 800, 480
    img = Image.new('RGB', (width, height), color='#FFFFFF')
    draw = ImageDraw.Draw(img)

    font_clock = get_font(24)
    font_day = get_font(12)
    font_meta = get_font(9)
    font_title = get_font(12)
    font_body = get_font(11)
    font_small = get_font(9)
    font_tiny = get_font(7)
    font_price = get_font(15)
    font_badge = get_font(13)

    tz_ba = timezone(timedelta(hours=-3))
    now_ba = datetime.now(tz_ba)
    window_end_ba = now_ba + timedelta(hours=8)
    
    pad = lambda n: str(n).zfill(2)
    time_str = f"{pad(now_ba.hour)}:{pad(now_ba.minute)}"
    window_str = f"{time_str} – {pad(window_end_ba.hour)}:{pad(window_end_ba.minute)}"
    
    dias = ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo"]
    meses = ["Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio", "Julio", "Agosto", "Septiembre", "Octubre", "Noviembre", "Diciembre"]
    day_str = f"{dias[now_ba.weekday()]}, {now_ba.day} de {meses[now_ba.month - 1]}"

    # Datos en RAM
    with CACHE_LOCK:
        wdata = WEATHER_CACHE.get("data")
        events = CALENDAR_CACHE.get("events", [])
        stocks = FINANCE_CACHE.get("data", [])

    # Clima
    temp_cur = "14°"
    desc_cur = "Mayormente despejado"
    range_cur = "Mín: 12° | Máx: 17°"
    sat_temp = "8°/13°"
    sun_temp = "4°/11°"

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
            
            min_c = round(wdata["daily"]["temperature_2m_min"][0])
            max_c = round(wdata["daily"]["temperature_2m_max"][0])
            range_cur = f"Mín: {min_c}° | Máx: {max_c}°"

            times = wdata["daily"]["time"]
            for i, ts in enumerate(times):
                d = datetime.strptime(ts, "%Y-%m-%d")
                if d.weekday() == 5:
                    sat_temp = f"{round(wdata['daily']['temperature_2m_min'][i])}°/{round(wdata['daily']['temperature_2m_max'][i])}°"
                if d.weekday() == 6:
                    sun_temp = f"{round(wdata['daily']['temperature_2m_min'][i])}°/{round(wdata['daily']['temperature_2m_max'][i])}°"
        except Exception:
            pass

    # 1. CABECERA (8, 8, 792, 74)
    draw.rounded_rectangle([8, 8, 792, 74], radius=6, outline="#000000", width=2, fill="#FFFFFF")
    draw.text((22, 26), time_str, font=font_clock, fill="#0044CC")
    draw.line([106, 18, 106, 64], fill="#000000", width=2)
    draw.text((116, 25), day_str, font=font_day, fill="#000000")
    draw.text((116, 45), "Buenos Aires", font=font_small, fill="#555555")

    draw.line([280, 18, 280, 64], fill="#000000", width=2)
    draw.text((294, 21), "PRONÓSTICO FIN DE SEMANA", font=font_meta, fill="#0044CC")
    draw.text((294, 43), f"SÁB: {sat_temp}", font=font_small, fill="#000000")
    draw.text((410, 43), f"DOM: {sun_temp}", font=font_small, fill="#000000")

    draw.line([515, 18, 515, 64], fill="#000000", width=2)
    
    # Sol de alto contraste con contorno negro
    cx, cy, r = 544, 41, 9
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill="#FFCC00", outline="#000000", width=2)
    for i in range(8):
        angle = i * (2 * math.pi / 8)
        x1 = cx + (r + 3) * math.cos(angle)
        y1 = cy + (r + 3) * math.sin(angle)
        x2 = cx + (r + 7) * math.cos(angle)
        y2 = cy + (r + 7) * math.sin(angle)
        draw.line([x1, y1, x2, y2], fill="#000000", width=2)

    draw.text((566, 26), temp_cur, font=font_clock, fill="#000000")
    draw.text((628, 20), "BUENOS AIRES", font=font_meta, fill="#0044CC")
    draw.text((628, 36), desc_cur, font=font_small, fill="#000000")
    draw.text((628, 50), range_cur, font=font_tiny, fill="#555555")

    # 2. PANEL AGENDA (8, 80, 448, 472)
    draw.rounded_rectangle([8, 80, 448, 472], radius=6, outline="#000000", width=2, fill="#FFFFFF")
    draw.text((20, 92), "PRÓXIMAS 8 HORAS", font=font_title, fill="#000000")
    draw.rounded_rectangle([335, 89, 436, 107], radius=3, fill="#0044CC")
    draw.text((342, 92), window_str, font=font_small, fill="#FFFFFF")
    draw.line([10, 114, 446, 114], fill="#000000", width=2)

    if not events:
        draw.rounded_rectangle([20, 160, 436, 380], radius=4, outline="#000000", width=1, fill="#FFFFFF")
        draw.text((120, 250), "Sin citas en las próximas 8 horas", font=font_title, fill="#008833")
        draw.text((95, 275), "Tu calendario no registra compromisos en este período", font=font_small, fill="#555555")
    else:
        y_evt = 120
        is_roomy = len(events) <= 4
        card_h = 68 if is_roomy else 52
        gap = 6 if is_roomy else 4
        
        for evt in events[:6]:
            draw.rounded_rectangle([16, y_evt, 440, y_evt + card_h], radius=4, outline="#000000", width=1, fill="#FFFFFF")
            draw.rectangle([16, y_evt, 21, y_evt + card_h], fill="#0044CC")
            
            t_text = f"{evt['start']} – {evt['end']}"
            draw.text((28, y_evt + 6), t_text, font=font_small, fill="#0044CC")
            
            dur = evt.get("duration", "30m")
            draw.rounded_rectangle([398, y_evt + 5, 432, y_evt + 19], radius=2, fill="#000000")
            draw.text((404, y_evt + 6), dur, font=font_tiny, fill="#FFFFFF")
            
            draw.text((28, y_evt + 23), evt["title"][:42], font=font_body, fill="#000000")
            
            sub = evt.get("location") or ("🍽️ Almuerzo" if "almuerzo" in evt["title"].lower() else "📍 Microsoft Teams")
            if evt.get("attendees"):
                sub = f"👤 {', '.join(evt['attendees'])}"
            draw.text((28, y_evt + (44 if is_roomy else 38)), sub[:46], font=font_tiny, fill="#555555")
            
            y_evt += card_h + gap

    # 3. PANEL FINANZAS (454, 80, 792, 472)
    draw.rounded_rectangle([454, 80, 792, 472], radius=6, outline="#000000", width=2, fill="#FFFFFF")
    draw.text((466, 92), "GOOGLE FINANCE", font=font_title, fill="#000000")
    draw.rounded_rectangle([720, 89, 780, 107], radius=3, fill="#000000")
    draw.text((727, 92), "CARTERA", font=font_small, fill="#FFFFFF")
    draw.line([456, 114, 790, 114], fill="#000000", width=2)

    card_w = 158
    card_h = 110
    row_gap = 6
    
    for idx, st in enumerate(stocks[:6]):
        r = idx // 2
        c = idx % 2
        x_c = 462 if c == 0 else 626
        y_c = 120 + r * (card_h + row_gap)
        
        draw.rounded_rectangle([x_c, y_c, x_c + card_w, y_c + card_h], radius=4, outline="#000000", width=1, fill="#FFFFFF")
        draw.text((x_c + 7, y_c + 6), st["sym"], font=font_body, fill="#000000")
        
        is_up = st.get("up", True)
        badge_bg = "#008833" if is_up else "#D60000"
        arrow = "▲" if is_up else "▼"
        chg_text = f"{arrow} {st['change']}"
        
        draw.rounded_rectangle([x_c + 84, y_c + 5, x_c + card_w - 6, y_c + 24], radius=3, fill=badge_bg)
        draw.text((x_c + 90, y_c + 7), chg_text, font=font_badge, fill="#FFFFFF")
        
        draw.text((x_c + 7, y_c + 26), f"${st['price']}", font=font_price, fill="#000000")
        
        # Velas de 60 días
        chart_x = x_c + 7
        chart_y = y_c + 48
        chart_w = 144
        chart_h = 36
        draw.rounded_rectangle([chart_x, chart_y, chart_x + chart_w, chart_y + chart_h], radius=3, fill="#FAFAFA", outline="#E5E7EB", width=1)
        draw.text((chart_x + 3, chart_y + 2), "60D", font=font_tiny, fill="#9CA3AF")
        
        candles = st.get("candles", [])
        if candles and len(candles) >= 2:
            all_l = [cd[2] for cd in candles if cd[2] is not None and cd[2] > 0]
            all_h = [cd[3] for cd in candles if cd[3] is not None and cd[3] > 0]
            if all_l and all_h:
                p_min, p_max = min(all_l), max(all_h)
                p_range = p_max - p_min if p_max > p_min else 1.0
                step = (chart_w - 8) / max(len(candles) - 1, 1)
                
                for ci, (op, hi, lo, cl) in enumerate(candles):
                    c_col = "#008833" if cl >= op else "#D60000"
                    cx = chart_x + 4 + ci * step
                    
                    def to_y(val):
                        return (chart_y + chart_h - 3) - ((val - p_min) / p_range * (chart_h - 6))
                    
                    y_h = to_y(hi)
                    y_l = to_y(lo)
                    y_o = to_y(op)
                    y_c = to_y(cl)
                    
                    draw.line([cx, y_h, cx, y_l], fill=c_col, width=1)
                    
                    bt = min(y_o, y_c)
                    bb = max(y_o, y_c)
                    if bb - bt < 1: bb = bt + 1
                    draw.rectangle([cx - 1, bt, cx + 1, bb], fill=c_col, outline=c_col)

        draw.line([x_c + 7, y_c + 90, x_c + card_w - 7, y_c + 90], fill="#000000", width=1)
        draw.text((x_c + 7, y_c + 94), st["name"][:25], font=font_tiny, fill="#000000")

    # Exportar PNG a buffer en memoria
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    png_bytes = buf.getvalue()
    
    with CACHE_LOCK:
        IMAGE_CACHE["bytes"] = png_bytes
        IMAGE_CACHE["timestamp"] = time.time()

    try:
        with open("dashboard.png", "wb") as f:
            f.write(png_bytes)
    except Exception:
        pass

    return png_bytes

def background_worker_loop():
    """Ejecuta periódicamente cada 15 minutos la actualización y generación del PNG"""
    print("[BG WORKER] Hilo de renderizado de dashboard iniciado...")
    while True:
        try:
            update_finance_data_sync()
            update_weather_data_sync()
            update_calendar_data_sync()
            render_png_dashboard()
            print(f"[BG WORKER] Imagen dashboard.png regenerada con éxito a las {datetime.now().strftime('%H:%M:%S')}")
        except Exception as e:
            print(f"[BG WORKER] Error: {e}")
        
        # Dormir 15 minutos entre ciclos completos
        time.sleep(900)

class ThreadedHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True

class RequestHandler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        # 1. Entrega inmediata del PNG en <2 milisegundos
        if self.path in ("/dashboard.png", "/image.png"):
            with CACHE_LOCK:
                png_bytes = IMAGE_CACHE.get("bytes")
            
            if not png_bytes and os.path.exists("dashboard.png"):
                with open("dashboard.png", "rb") as f:
                    png_bytes = f.read()

            if png_bytes:
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(png_bytes)))
                self.send_header("Connection", "close")
                self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(png_bytes)
                return
            else:
                # Si recién arranca, genera la primera imagen al vuelo
                png_bytes = render_png_dashboard()
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(png_bytes)))
                self.send_header("Connection", "close")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(png_bytes)
                return

        # 2. Silenciar favicon
        elif self.path == "/favicon.ico":
            self.send_response(204)
            self.send_header("Connection", "close")
            self.end_headers()
            return

        # 3. APIs auxiliares
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

        # 4. Vista web en / que muestra directamente la imagen generada
        elif self.path in ("/", "/index.html"):
            html = """<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=800, height=480, initial-scale=1.0">
  <title>reTerminal E1002 - Dashboard</title>
  <style>
    * { margin:0; padding:0; box-sizing:border-box; background:#FFFFFF; }
    body { width:800px; height:480px; overflow:hidden; }
    img { width:800px; height:480px; display:block; }
  </style>
</head>
<body>
  <img src="/dashboard.png" alt="Dashboard E-Paper">
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

if __name__ == "__main__":
    print(f"Servidor PNG de alta velocidad escuchando en el puerto {PORT}...")
    
    # Iniciar ciclo en segundo plano
    bg_thread = threading.Thread(target=background_worker_loop, daemon=True)
    bg_thread.start()
    
    server = ThreadedHTTPServer(("", PORT), RequestHandler)
    server.serve_forever()
