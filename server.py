#!/usr/bin/env python3
"""
Servidor para reTerminal E1002
- Conecta directamente con el feed iCal de Outlook 365 (Bitali)
- Extrae todas las reuniones (únicas y recurrentes) en las próximas 8 horas
- Extrae organizadores y nombres reales (CN) para cada cita (ej: Dolores Giardelli, Camila Ortiz)
- Lee exclusivamente los activos desde tu Google Sheet 'Activos' (ID: 1t1l4MjlXuid0ljh2zZuUC-5mAyHVZyQUrjV-NtfXKx4)
- Endpoint de diagnóstico en /api/debug_calendar
"""

import http.server
import socketserver
import json
import urllib.request
import re
import os
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

            tickers.append({
                "sym": api_sym,
                "label": display_label,
                "name": display_label
            })
            if len(tickers) == 6:
                break

        return tickers
    except Exception as e:
        print(f"[FINANCE] Error al leer Google Sheet: {e}")
        return []

def fetch_finance_data():
    tickers = get_tickers_from_sheet()
    if not tickers:
        return []

    results = []
    headers = {'User-Agent': 'Mozilla/5.0'}

    for t in tickers:
        sym = t["sym"]
        label = t.get("label", sym)
        try:
            url = f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}?interval=1d&range=1d"
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read().decode())
                meta = data["chart"]["result"][0]["meta"]
                price = meta.get("regularMarketPrice", 0)
                prev_close = meta.get("previousClose", price)
                short_name = meta.get("shortName", meta.get("symbol", label))
                change_pct = ((price - prev_close) / prev_close) * 100 if prev_close else 0.0
                
                up = change_pct >= 0
                sign = "+" if up else ""
                price_str = f"{price:,.2f}" if price >= 1000 else f"{price:.2f}"
                
                results.append({
                    "sym": label,
                    "name": short_name,
                    "price": price_str,
                    "change": f"{sign}{change_pct:.2f}%",
                    "up": up
                })
        except Exception:
            results.append({
                "sym": label,
                "name": label,
                "price": "N/A",
                "change": "0.00%",
                "up": True
            })

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

def get_calendar_data_and_debug():
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
        "matched_events": 0,
        "error": None
    }

    events = []

    try:
        req = urllib.request.Request(
            ICAL_URL,
            headers={
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                'Accept': 'text/calendar, text/plain, */*'
            }
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            debug_info["http_status"] = resp.status
            content = resp.read().decode('utf-8', errors='ignore')
            debug_info["content_length"] = len(content)

        unfolded = re.sub(r'\r?\n[ \t]', '', content)
        raw_events = re.findall(r'BEGIN:VEVENT(.*?)END:VEVENT', unfolded, re.DOTALL | re.IGNORECASE)
        debug_info["total_vevents"] = len(raw_events)

        weekday_map = {0: "MO", 1: "TU", 2: "WE", 3: "TH", 4: "FR", 5: "SA", 6: "SU"}
        today_code = weekday_map[now_ba.weekday()]

        for raw in raw_events:
            summary_m = re.search(r'SUMMARY(?:;[^:\r\n]*)?:(.*?)\r?\n', raw, re.IGNORECASE)
            summary = summary_m.group(1).strip() if summary_m else "Reunión programada"
            summary = summary.replace('\\,', ',').replace('\\;', ';')

            dtstart_m = re.search(r'DTSTART(?:;[^:\r\n]*)?:([0-9TZ]+)', raw, re.IGNORECASE)
            dtend_m = re.search(r'DTEND(?:;[^:\r\n]*)?:([0-9TZ]+)', raw, re.IGNORECASE)
            rrule_m = re.search(r'RRULE:(.*?)\r?\n', raw, re.IGNORECASE)
            
            # Extraer organizadores y asistentes con su nombre real (CN)
            org_m = re.search(r'ORGANIZER(?:;[^:\r\n]*)?;CN="?([^":;\r\n]+)"?', raw, re.IGNORECASE)
            org_name = org_m.group(1).strip() if org_m else None
            
            att_cns = re.findall(r'ATTENDEE(?:;[^:\r\n]*)?;CN="?([^":;\r\n]+)"?', raw, re.IGNORECASE)
            emails = re.findall(r'(?:ATTENDEE|ORGANIZER).*?(?:mailto:)?([a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+)', raw, re.IGNORECASE)

            people = []
            if org_name and org_name not in people:
                people.append(org_name)
            for a in att_cns:
                clean_a = a.strip()
                if clean_a and clean_a not in people:
                    people.append(clean_a)
            if not people and emails:
                people = [e.split('@')[0].capitalize() for e in emails]

            if dtstart_m:
                dt_start = parse_ical_dt(dtstart_m.group(1), tz_ba)
                if dtend_m:
                    dt_end = parse_ical_dt(dtend_m.group(1), tz_ba)
                else:
                    dt_end = dt_start + timedelta(minutes=30)
                duration = dt_end - dt_start

                target_start = None
                target_end = None

                # 1. Evento único fechado hoy
                if dt_end >= now_ba and dt_start <= window_end_ba:
                    target_start = dt_start
                    target_end = dt_end

                # 2. Evento recurrente (RRULE)
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
                        "attendees": people
                    })

        events.sort(key=lambda x: x["start"])
        debug_info["matched_events"] = len(events)
        print(f"[CALENDAR] {len(events)} citas activas encontradas")
    except Exception as e:
        debug_info["error"] = str(e)
        print(f"[CALENDAR] Error: {e}")

    return events, debug_info

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
        elif self.path == "/api/debug_calendar":
            events, debug_info = get_calendar_data_and_debug()
            debug_info["events"] = events
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps(debug_info, indent=2).encode())
            return
        elif self.path in ("/", "/index.html"):
            if os.path.exists("index.html"):
                self.path = "/index.html"
            else:
                self.path = "/dashboard_perfect.html"
        return super().do_GET()

if __name__ == "__main__":
    print(f"Servidor activo en el puerto {PORT}")
    with socketserver.TCPServer(("", PORT), RequestHandler) as httpd:
        httpd.serve_forever()
