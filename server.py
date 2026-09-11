#!/usr/bin/env python3
"""
Servidor para reTerminal E1002
"""

import http.server
import socketserver
import json
import urllib.request
import re
import os
from datetime import datetime, timedelta, timezone

PORT = int(os.environ.get("PORT", 5000))

# Google Calendar (iCal público o URL original de suscripción .ics)
ICAL_URL = os.environ.get(
    "ICAL_URL",
    "https://calendar.google.com/calendar/ical/gpp5lqo37705ugc0uacmnkgmoi3iqtp1%40import.calendar.google.com/public/basic.ics"
)

# Google Sheet 'Activos' de Pablo
SHEET_ID = "1t1l4MjlXuid0ljh2zZuUC-5mAyHVZyQUrjV-NtfXKx4"
SHEET_CSV_URL = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/export?format=csv"

def get_tickers_from_sheet():
    default_tickers = [
        {"sym": "SPY", "label": "SPY", "name": "S&P 500 ETF"},
        {"sym": "QQQ", "label": "QQQ", "name": "Invesco Nasdaq"},
        {"sym": "MELI", "label": "MELI", "name": "MercadoLibre"},
        {"sym": "VIST", "label": "VIST", "name": "Vista Energy"},
        {"sym": "BRK-B", "label": "BRK.B", "name": "Berkshire Cl B"},
        {"sym": "BTC-USD", "label": "BTC-USD", "name": "Bitcoin (USD)"}
    ]
    if not SHEET_CSV_URL:
        return default_tickers
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
        return tickers if len(tickers) > 0 else default_tickers
    except Exception:
        return default_tickers

def fetch_finance_data():
    tickers = get_tickers_from_sheet()
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
                    "sym": label, "name": short_name, "price": price_str,
                    "change": f"{sign}{change_pct:.2f}%", "up": up
                })
        except Exception:
            results.append({"sym": label, "name": t.get("name", label), "price": "N/A", "change": "0.00%", "up": True})
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

def fetch_calendar_events():
    events = []
    tz_ba = timezone(timedelta(hours=-3))
    now_ba = datetime.now(tz_ba)
    window_end_ba = now_ba + timedelta(hours=8)

    try:
        req = urllib.request.Request(ICAL_URL, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=8) as resp:
            content = resp.read().decode('utf-8', errors='ignore')

        raw_events = re.findall(r'BEGIN:VEVENT(.*?)END:VEVENT', content, re.DOTALL)
        for raw in raw_events:
            summary_m = re.search(r'SUMMARY:(.*?)\r?\n', raw)
            dtstart_m = re.search(r'DTSTART.*?:([0-9TZ]+)', raw)
            dtend_m = re.search(r'DTEND.*?:([0-9TZ]+)', raw)
            attendees = re.findall(r'ATTENDEE.*?(?:mailto:)?([a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+)', raw, re.IGNORECASE)

            if summary_m and dtstart_m:
                summary = summary_m.group(1).strip()
                dt_start = parse_ical_dt(dtstart_m.group(1), tz_ba)
                
                if dtend_m:
                    dt_end = parse_ical_dt(dtend_m.group(1), tz_ba)
                else:
                    dt_end = dt_start + timedelta(minutes=30)

                # Incluye citas en curso o que inicien en las próximas 8 horas
                if dt_end >= now_ba and dt_start <= window_end_ba:
                    dur_min = int((dt_end - dt_start).total_seconds() / 60)
                    dur_str = f"{dur_min}m" if dur_min < 60 else f"{dur_min//60}h"
                    events.append({
                        "title": summary,
                        "start": dt_start.strftime("%H:%M"),
                        "end": dt_end.strftime("%H:%M"),
                        "duration": dur_str,
                        "attendees": attendees
                    })

        events.sort(key=lambda x: x["start"])
        print(f"[CALENDAR] Éxito: {len(events)} citas en la ventana de 8h")
    except Exception as e:
        print(f"[CALENDAR] Error al conectar o parsear iCal: {e}")

    return events

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
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps(fetch_calendar_events()).encode())
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
