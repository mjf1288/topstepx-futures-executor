# Setup — TopstepX Futures Executor

Fresh-machine setup, from `git clone` to running engine, in ~5 minutes.

---

## Prerequisites

- **Python 3.11 or newer** (3.12/3.13 recommended, 3.14 works but is bleeding-edge)
- **ProjectX / TopstepX API Access subscription** — $29/mo (or $14.50/mo with `topstep` promo code) via https://topstep.com
- **A TopstepX account** with Personal API activated and an API key generated

---

## Step 1: Clone the repo

```bash
git clone https://github.com/mjf1288/topstepx-futures-executor.git ~/Desktop/topstepx-futures-executor
cd ~/Desktop/topstepx-futures-executor
```

**Important:** the current working engine lives on the `rebuild-data-layer` branch, not `main`. Check it out:

```bash
git checkout rebuild-data-layer
```

`main` still holds pre-July-2026 code that depends on the abandoned `project-x-py` library and no longer works.

---

## Step 2: Create Python virtual environment + install dependencies

```bash
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

If you're on **Windows** (untested but should work), use `venv\Scripts\activate` instead of `source venv/bin/activate`.

---

## Step 3: Create your `.env` file

```bash
cp .env.example .env
```

Open `.env` in an editor and fill in:

- `PROJECT_X_USERNAME` — your Topstep username (same as your Topstep login)
- `PROJECT_X_API_KEY` — the API key generated from Topstep's API Access page
- `PROJECT_X_ACCOUNT_NAME` — the account name shown in TopstepX (e.g. `EXPRESS-V2-CT-163901-94668864`)

**Never commit `.env`** — it's already listed in `.gitignore`.

---

## Step 4: Verify the setup

Run the standalone REST-layer test:

```bash
python test_topstep_api.py
```

Expected output: rows of `✓` checks and a final `ALL TESTS PASSED ✓` banner. This confirms:
- Auth against ProjectX Gateway works
- The account name in `.env` matches a real account
- Historical bar retrieval works for MES + MNQ U26 contracts

If it fails on the first check, your credentials or account name are wrong. Fix `.env` and re-run.

Then run the streaming test during an active session (RTH or Globex):

```bash
python test_topstep_stream.py --minutes 5
```

Expected: at least one bar closes cleanly, no `bounded_statistics width 8` errors. This confirms the SignalR WebSocket + tick aggregator is working.

---

## Step 5: Dry-run the engine

Before ever going live on a new machine:

```bash
python realtime_engine.py --mes buy --dry-run
```

Watch it seed CDM/PDM/CMM/PMM values, print the `EXECUTION-ONLY` banner, and start streaming. No orders will be placed. `Ctrl+C` to stop.

---

## Step 6: Go live

When you're ready:

```bash
python realtime_engine.py --mnq buy --mes buy
```

See [README.md](README.md) or the master command sheet for all combos (buy/sell × MNQ/MES/MYM/MGC × single vs multi).

---

## Engine state at the current commit

- **Branch:** `rebuild-data-layer`
- **Cap:** 4 contracts per instrument (positions + working orders combined)
- **Eligible levels:** CDM, PDM, CMM. PMM is disabled at the entry layer until 2026-08-01 (waiting for U26 to accumulate a full prior-month of native data — see commit `ad3c818`).
- **Risk management:** EXECUTION-ONLY. Engine places entry limits only. YOU attach stops and targets manually on the TopstepX UI.
- **Front-month contracts:** MNQ/MES/MYM = U26 (rolls ~Sept 11), MGC = Q26 (rolls ~Aug 26).

---

## Troubleshooting

**`Account 'X' not found`**
Your `PROJECT_X_ACCOUNT_NAME` doesn't match any real account. The error message lists available accounts. Copy one of those into `.env`.

**`bounded_statistics width 8 to width 6`**
You're running an old branch or old code. Confirm you're on `rebuild-data-layer` and that `pip show project-x-py` returns "not installed" — if it's installed, `pip uninstall project-x-py`.

**Engine seeds cleanly but no orders place**
Most likely price is not close enough to CDM/PDM/CMM to trigger eligibility. In BUY mode, engine places entries only at levels BELOW price. In SELL mode, only levels ABOVE. If price is far from all levels, no orders is correct behavior.

**~40 stale orders appear after multiple restarts**
This shouldn't happen anymore (execution-only mode was pushed 2026-07-23 specifically to prevent orphan bracket accumulation). If you see this, cancel all orders manually on TopstepX UI and grep the git log for what regressed.

---

## Portability checklist

If you're setting this up on a different machine:

- [ ] Python 3.11+ installed
- [ ] Repo cloned to `~/Desktop/topstepx-futures-executor`
- [ ] Checked out `rebuild-data-layer` branch
- [ ] `venv` created and activated
- [ ] `pip install -r requirements.txt` succeeded
- [ ] `.env` created with real credentials
- [ ] `test_topstep_api.py` passes
- [ ] `test_topstep_stream.py` shows ≥1 bar close in 5-15 min during a live session
- [ ] Dry-run seeds cleanly and shows `EXECUTION-ONLY` banner

If all boxes check, you're ready to trade.

---

## Topstep Terms of Service

Topstep prohibits running automation on a **VPS, VPN, or remote server**. Trading activity must originate from your personal device. Running this engine in the cloud can result in account suspension. **Only run on your local machine.**
