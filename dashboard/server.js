/*
 * Tzu Strategic Momentum Futures — Regime Dashboard
 *
 * Reads the JSON state files that run_regime_session.py writes:
 *   regime_state.json    → per-instrument regime (BUY/SELL, DSS reading, latched)
 *   orders_state.json    → open bracket orders (entry / stop / target)
 *   trade_log.json       → closed trades (P&L, running total)
 *   combine_tracker.json → account balance, MLL floor, today's P&L
 *
 * Zero coupling to the engine — engine writes JSON, dashboard polls the files.
 */

const express = require('express');
const path = require('path');
const fs = require('fs');

const app = express();
const PORT = Number(process.env.PORT || 8095);
const ENGINE_ROOT = process.env.ENGINE_ROOT
  ? path.resolve(process.env.ENGINE_ROOT)
  : path.resolve(__dirname, '..');

app.use(express.static(__dirname));

function readJson(name, fallback = null) {
  try {
    const p = path.join(ENGINE_ROOT, name);
    if (!fs.existsSync(p)) return fallback;
    return JSON.parse(fs.readFileSync(p, 'utf8'));
  } catch (e) {
    console.warn(`[dashboard] failed to read ${name}: ${e.message}`);
    return fallback;
  }
}

// Compute next fire time at 1am / 9am / 6pm ET (matches daemon schedule)
function nextFireET() {
  const SCHEDULE = [1, 9, 18];
  const now = new Date();
  // Convert to ET
  const etParts = new Intl.DateTimeFormat('en-US', {
    timeZone: 'America/New_York',
    hour12: false,
    year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', second: '2-digit',
  }).formatToParts(now).reduce((a, p) => (a[p.type] = p.value, a), {});
  const etYear = Number(etParts.year), etMonth = Number(etParts.month) - 1, etDay = Number(etParts.day);
  const etHour = Number(etParts.hour), etMin = Number(etParts.minute), etSec = Number(etParts.second);
  // Look up-to-tomorrow for the next fire hour
  const nowET = new Date(Date.UTC(etYear, etMonth, etDay, etHour, etMin, etSec));
  for (let dayOffset = 0; dayOffset < 2; dayOffset++) {
    for (const h of SCHEDULE) {
      const fireET = new Date(Date.UTC(etYear, etMonth, etDay + dayOffset, h, 0, 0));
      if (fireET > nowET) {
        return {
          iso: fireET.toISOString(),
          ms_until: fireET - nowET,
          hour_et: h,
        };
      }
    }
  }
  return null;
}

app.get('/api/snapshot', (req, res) => {
  const regime = readJson('regime_state.json', { instruments: {} });
  const orders = readJson('orders_state.json', { pending_limits: {} });
  const trades = readJson('trade_log.json', []);
  const tracker = readJson('combine_tracker.json', {});

  // Today's ET date
  const todayET = new Intl.DateTimeFormat('en-CA', {
    timeZone: 'America/New_York', year: 'numeric', month: '2-digit', day: '2-digit',
  }).format(new Date());

  // Daily P&L per symbol (from trades that closed today ET)
  const todayTrades = trades.filter(t => {
    const exitDate = new Date(t.exit_time || t.entry_time);
    const etDate = new Intl.DateTimeFormat('en-CA', {
      timeZone: 'America/New_York', year: 'numeric', month: '2-digit', day: '2-digit',
    }).format(exitDate);
    return etDate === todayET;
  });

  const dailyPnl = todayTrades.reduce((sum, t) => sum + (t.pnl || 0), 0);
  const dailyPnlBySymbol = todayTrades.reduce((m, t) => {
    m[t.symbol] = (m[t.symbol] || 0) + (t.pnl || 0);
    return m;
  }, {});

  // Latest N closed trades for the tape
  const recentTrades = trades.slice(-20).reverse();

  // Cumulative equity curve
  const equity = [];
  let running = 0;
  for (const t of trades) {
    running += t.pnl || 0;
    equity.push({ t: t.exit_time || t.entry_time, equity: running, symbol: t.symbol });
  }

  res.json({
    now: new Date().toISOString(),
    engine_root: ENGINE_ROOT,
    next_fire: nextFireET(),
    regime,
    orders,
    tracker,
    daily: {
      pnl: dailyPnl,
      pnl_by_symbol: dailyPnlBySymbol,
      trade_count: todayTrades.length,
      cap: 1400,
    },
    recent_trades: recentTrades,
    equity_curve: equity.slice(-100),  // last 100 points for the sparkline
    total_trades: trades.length,
  });
});

app.get('/api/health', (req, res) => {
  const files = ['regime_state.json', 'orders_state.json', 'trade_log.json', 'combine_tracker.json'];
  const status = {};
  for (const f of files) {
    const p = path.join(ENGINE_ROOT, f);
    if (fs.existsSync(p)) {
      const stat = fs.statSync(p);
      status[f] = { exists: true, mtime: stat.mtime, age_sec: Math.floor((Date.now() - stat.mtime) / 1000) };
    } else {
      status[f] = { exists: false };
    }
  }
  res.json({ engine_root: ENGINE_ROOT, files: status });
});

app.listen(PORT, () => {
  console.log(`Tzu Futures Regime Dashboard on http://localhost:${PORT}`);
  console.log(`Engine root: ${ENGINE_ROOT}`);
});
