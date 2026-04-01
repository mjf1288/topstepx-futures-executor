"""
Rebuild Tzu Strategic Momentum signal chart data.
Fetches latest 8h bars, computes DSS signals with latching, CMM gate,
and updates data.json + re-embeds into index.html.

Run after each regime session to keep the dashboard current.
"""

import json, os, sys, re
from datetime import datetime, timezone, timedelta
import requests
import pytz

# Setup
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
FUTURES_DIR = os.path.join(os.path.dirname(SCRIPT_DIR), 'futures_mean_levels')
sys.path.insert(0, FUTURES_DIR)

from dotenv import load_dotenv
load_dotenv(os.path.join(FUTURES_DIR, '.env'))

from trend_filters import DSSBressert

BASE = 'https://api.topstepx.com/api'
ct = pytz.timezone('America/Chicago')
now = datetime.now(timezone.utc)

SYMBOLS = {
    'MES': ('CON.F.US.MES.M26', 'CON.F.US.MES.H26', 0.25),
    'MNQ': ('CON.F.US.MNQ.M26', 'CON.F.US.MNQ.H26', 0.25),
    'MYM': ('CON.F.US.MYM.M26', 'CON.F.US.MYM.H26', 1.0),
}


def auth():
    r = requests.post(f'{BASE}/Auth/loginKey', json={
        'userName': os.environ['PROJECT_X_USERNAME'],
        'apiKey': os.environ['PROJECT_X_API_KEY']
    })
    return r.json()['token']


def fetch_bars(token, contract, unit, unit_number, limit=5000, days=200):
    headers = {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}
    payload = {
        "contractId": contract, "live": False,
        "startTime": (now - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "endTime": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "unit": unit, "unitNumber": unit_number,
        "limit": limit, "includePartialBar": True,
    }
    resp = requests.post(f'{BASE}/History/retrieveBars', json=payload, headers=headers)
    return resp.json().get('bars', [])


def stitch(old_bars, new_bars):
    if not old_bars or not new_bars:
        return new_bars or old_bars or []
    old_bars.sort(key=lambda x: x['t'])
    new_bars.sort(key=lambda x: x['t'])
    gap = new_bars[0]['o'] - old_bars[-1]['c']
    for b in old_bars:
        b['o'] += gap; b['h'] += gap; b['l'] += gap; b['c'] += gap
    ft = new_bars[0]['t']
    return [b for b in old_bars if b['t'] < ft] + new_bars


def main():
    print("Authenticating...")
    token = auth()
    all_data = {}

    for sym, (curr, prior, tick_size) in SYMBOLS.items():
        print(f"\n--- {sym} ---")

        # 1. Fetch & stitch 8h bars
        old_8h = fetch_bars(token, prior, 3, 8)
        new_8h = fetch_bars(token, curr, 3, 8)
        bars_8h = stitch(old_8h, new_8h)
        bars_8h.sort(key=lambda x: x['t'])
        print(f"  8h bars: {len(bars_8h)}")

        # 2. Compute DSS at each bar
        highs = [b['h'] for b in bars_8h]
        lows = [b['l'] for b in bars_8h]
        closes = [b['c'] for b in bars_8h]

        dss_signals = []
        for i in range(len(bars_8h)):
            dss_i = DSSBressert()
            dss_i.calculate(highs[:i+1], lows[:i+1], closes[:i+1])
            dss_signals.append(dss_i.get_signal())

        # 3. Compute latched mode
        latched_modes = []
        current_latch = None
        for sig in dss_signals:
            if sig >= 4:
                current_latch = 'BUY'
            elif sig <= -4:
                current_latch = 'SELL'
            latched_modes.append(current_latch or 'NEUTRAL')

        # 4. Fetch hourly bars for CMM
        old_1h = fetch_bars(token, prior, 3, 1, 5000, 200)
        new_1h = fetch_bars(token, curr, 3, 1, 5000, 200)
        all_1h = stitch(old_1h, new_1h)
        all_1h.sort(key=lambda x: x['t'])
        print(f"  1h bars for CMM: {len(all_1h)}")

        # Group hourly by month and day
        month_data = {}
        day_data = {}
        for b in all_1h:
            ts = datetime.fromisoformat(b['t']).astimezone(ct)
            ym = (ts.year, ts.month)
            month_data.setdefault(ym, []).append((ts, b['c']))
            day_data.setdefault(ts.date(), []).append((ts, b['c']))
        sorted_dates = sorted(day_data.keys())

        # 5. Compute all mean levels + execution markers at each 8h bar
        chart_data = []
        for i, b in enumerate(bars_8h):
            bar_ts = datetime.fromisoformat(b['t']).astimezone(ct)
            bar_ym = (bar_ts.year, bar_ts.month)
            bar_date = bar_ts.date()
            price = b['c']

            # CMM: running mean of current month
            cmm = None
            if bar_ym in month_data:
                mc = [c for ts, c in month_data[bar_ym] if ts <= bar_ts]
                if mc: cmm = round(sum(mc) / len(mc), 2)

            # CDM: running mean of current day
            cdm = None
            if bar_date in day_data:
                dc = [c for ts, c in day_data[bar_date] if ts <= bar_ts]
                if dc: cdm = round(sum(dc) / len(dc), 2)

            # PDM: mean of previous day
            pdm = None
            di = sorted_dates.index(bar_date) if bar_date in sorted_dates else -1
            if di > 0:
                pc = [c for ts, c in day_data[sorted_dates[di - 1]]]
                if pc: pdm = round(sum(pc) / len(pc), 2)

            # PMM: mean of previous month
            pmm = None
            prev_ym = (bar_ym[0], bar_ym[1] - 1) if bar_ym[1] > 1 else (bar_ym[0] - 1, 12)
            if prev_ym in month_data:
                pmc = [c for ts, c in month_data[prev_ym]]
                if pmc: pmm = round(sum(pmc) / len(pmc), 2)

            # Structure gate: PMM for first 3 days of month, CMM after
            mode = latched_modes[i]
            eff = mode
            gate = cmm
            if bar_date.day <= 3 and pmm is not None:
                gate = pmm
            if gate is not None and mode in ('BUY', 'SELL'):
                if mode == 'BUY' and price < gate:
                    eff = 'BLOCKED'
                elif mode == 'SELL' and price > gate:
                    eff = 'BLOCKED'

            # Execution level (closest eligible)
            el = ep = None
            if eff in ('BUY', 'SELL'):
                cands = []
                for ln, lp in [('CDM', cdm), ('PDM', pdm), ('CMM', cmm), ('PMM', pmm)]:
                    if lp is None: continue
                    if eff == 'BUY' and lp < price:
                        cands.append((abs(price - lp), ln, lp))
                    elif eff == 'SELL' and lp > price:
                        cands.append((abs(lp - price), ln, lp))
                if cands:
                    cands.sort()
                    _, el, ep = cands[0]
                    ep = round(round(ep / tick_size) * tick_size, 4)

            chart_data.append({
                't': b['t'],
                'o': round(b['o'], 2), 'h': round(b['h'], 2),
                'l': round(b['l'], 2), 'c': round(b['c'], 2),
                'dss': dss_signals[i],
                'mode': mode, 'eff': eff,
                'cmm': cmm, 'cdm': cdm, 'pdm': pdm, 'pmm': pmm,
                'el': el, 'ep': ep,
            })

        all_data[sym] = chart_data
        blocked = sum(1 for d in chart_data if d['eff'] == 'BLOCKED')
        with_ep = sum(1 for d in chart_data if d.get('ep'))
        print(f"  {sym}: {len(chart_data)} bars, {blocked} CMM-blocked, {with_ep} with entries")

    # Save data.json
    data_path = os.path.join(SCRIPT_DIR, 'data.json')
    with open(data_path, 'w') as f:
        json.dump(all_data, f)
    print(f"\nSaved {data_path}")

    # Update embedded data in index.html
    html_path = os.path.join(SCRIPT_DIR, 'index.html')
    if os.path.exists(html_path):
        with open(html_path) as f:
            html = f.read()

        data_str = json.dumps(all_data)
        lines = html.split('\n')
        new_lines = []
        for line in lines:
            if line.strip().startswith('const DATA = '):
                new_lines.append(f'const DATA = {data_str};')
            else:
                new_lines.append(line)

        with open(html_path, 'w') as f:
            f.write('\n'.join(new_lines))
        print(f"Updated {html_path} with fresh data")
    else:
        print(f"WARNING: {html_path} not found, skipping HTML update")

    print("\nDone.")


if __name__ == '__main__':
    main()
