# soccer-papi — Soccer Arbitrage Sniper ⚽📈

Detects **arbitrage opportunities** in soccer across multiple bookmakers and exchanges
using the [OddsPapi v4 API](https://oddspapi.io/us/docs), then:

1. **Appends every arb** to `data/arbitrage_opportunities.csv` (committed to the repo).
2. **Sends the top 3** to a Telegram group.
3. **Prints the full arbitrage math** to the GitHub Actions logs (odds per leg, implied
   probability sum `S`, ROI, stake split, and max stake `T_max`).

It runs **entirely on GitHub Actions** — nothing needs to run on your machine. It is
deliberately **frugal**: ~1 billable API request per scan (free tier = 250 requests/month),
with a pre-flight budget guard that refuses to run when the quota is nearly gone.

Current scope: **international friendlies, 5–8 June 2026 (UTC)**. The design generalises to
the **FIFA World Cup** by changing config / tournament IDs only.

> ⚠️ Personal informational tooling, **not financial advice**. See [Risk caveats](#risk-caveats).

---

## How it works (one scan cycle)

```
/v4/account (free)  ──►  budget guard (exit cleanly if quota nearly gone)
        │
cached catalogs  ──►  markets (MECE classifier) · bookmakers (clone map) · tournaments (friendlies IDs)
        │
/v4/fixtures (≤1/tournament, cached ≤6h)  ──►  fixtureId → human team names
        │
/v4/odds-by-tournaments × N books (1 billable PER book)  ──►  merged under one canonical fixtureId
        │
normalize  ──►  per fixture / per marketId / per outcomeId  best-price table across books
        │
arbitrage engine (per MECE market)
   • clone-aware best-odds selection (a book + its clone can't be two legs)
   • S = Σ 1/oᵢ_eff   ·   arb iff S < 1   ·   ROI = 1/S − 1
   • stakeᵢ = T·pᵢ/S  ·  T_max = minᵢ(Lᵢ·oᵢ_eff·S)  ·  profit = T_max·(1/S − 1)
        │
   ├─ real arbs   (legs only from `actionable` funded books)
   └─ shadow arbs (legs from any `tracked` book → which unfunded book to fund next)
        │
CSV append (signature dedup)  +  Telegram top 3  +  full-calc logs + summary
        │
git commit data/ back to main  ([skip ci])
```

> **Important — free/standard tier reality.** The public docs say `bookmakers` is optional and
> omitting it returns all books in one call. In practice the live free/standard subscription
> **rejects that** (`400 INVALID_PARAMETER`) and returns **exactly one bookmaker per call** via a
> singular `bookmaker` param — and only for the books your subscription grants. So cross-book
> arbitrage costs **one request per book**: the scanner fetches up to `budget.max_books_per_cycle`
> books (actionable first), merges them onto each canonical fixture, then computes real + shadow
> arbs. The bot logs which books your plan grants (read for free from `/v4/account`) and **exits
> without spending any odds requests if fewer than 2 usable books are available** (you can't arb
> one book).

---

## The arbitrage math

Work in **decimal odds**. For a MECE market with outcomes `i = 1..n`, take the best
**effective** odds per outcome across eligible books (and which book/limit gave it):

```
oᵢ_eff = 1 + (oᵢ − 1)·(1 − c_book)     # commission c on net winnings (exchanges); sportsbooks c = 0
pᵢ     = 1 / oᵢ_eff
S      = Σ pᵢ
arb    ⇔  S < 1
ROI    = (1/S) − 1
stakeᵢ = T · pᵢ / S                     # equalises payouts: every leg returns T/S
T_max  = minᵢ ( Lᵢ · oᵢ_eff · S )       # binding leg uses its full limit Lᵢ
profit = T_max · ((1/S) − 1)
```

**Worked example** (reproduced by `tests/test_arbitrage.py`): Over 2.5 @ 2.10 (limit 1500) vs
Under 2.5 @ 2.05 (limit 5000) → `S = 0.9640`, **ROI 3.74%**, **T_max = 3036.6**,
stakes 1500.0 / 1536.6, **guaranteed profit 113.4**.

---

## Markets scanned

All markets are discovered from `/v4/markets` and classified into MECE back-arb families.
**Double Chance is excluded** (it overlaps the 1x2 outcomes). Outcomes are only matched
within the **same `marketId`**, which already encodes period + line + type — so lines and
periods never get mixed.

| Family | Outcomes | Notes |
|---|---|---|
| `1x2` Full Time Result | 1 / X / 2 | three independent legs (not double chance) |
| `totals` Over/Under | Over / Under | every line, every period |
| `btts` Both Teams To Score | Yes / No | |
| `asian_handicap` | Home / Away | whole + half lines; quarter lines skipped by default |
| `euro_handicap` 3-way | 1 / X / 2 + line | |
| `dnb` Draw No Bet | Home / Away | push (stake refunded) on draw — flagged |
| `odd_even` | Odd / Even | |
| `team_totals` | Over / Under | home & away team totals |

First-half / second-half variants are picked up automatically via the market's `period`.
A market is scanned **only if every one of its outcomes is priced**; incomplete markets are
skipped. The distinct `marketId`s seen in each feed are logged so coverage is verifiable —
if a target market is missing, raise `api.odds_verbosity` in `config.yaml`.

---

## Bookmakers

Two lists in `config.yaml`:

- **`actionable`** — books you can actually bet (funded accounts). **Real arbs use only these.**
  Default: `pinnacle, 1xbet, kalshi, polymarket`.
- **`tracked`** — everything you want stats on. **Shadow arbs** may use any tracked book; the
  end-of-run summary ranks unfunded books by how many shadow arbs they completed, so you can
  decide **which 2 crypto books to fund next**.

Jurisdiction is **ignored entirely** — every listed book is treated as fully accessible.

- **Clone dedup** (`cloneOf`): a book and its clone share identical lines and can never form
  two legs of one arb. The engine unions clone lineages and keeps the better (higher limit /
  actionable) side.
- **Exchanges** (`kalshi`, `polymarket`, `sx-bet`, …): liquidity comes from `exchangeMeta` /
  `limit`; if unknown, the arb is flagged `low_confidence`. A per-book `commission` is applied
  to effective odds (default 0).

---

## Safety filters

- **Staleness** — legs whose `changedAt` is older than `max_leg_age_minutes` (default 20) are dropped.
- **ROI ceiling** — arbs above `roi_suspicious_pct` (default 8%) are flagged `suspicious`
  (likely a stale/error line), still recorded, de-prioritised for Telegram.
- **ROI floor / min stake** — only arbs with ROI ≥ `min_roi_pct` (0.5%) **and** `T_max ≥
  min_total_stake` (20) are kept.
- **Completeness** — every outcome of the market must be priced.
- **Status** — finished / cancelled / already-kicked-off fixtures are skipped (the struck-through
  cancelled fixture in the June screenshots is excluded via `statusId`).

---

## CSV output — `data/arbitrage_opportunities.csv`

Header is created on first run; rows are **appended** (history is never truncated). Rows are
keyed by a stable `signature` (fixture + market + line + sorted leg books + rounded odds); a
signature re-seen within `csv_dedup_minutes` (90) **updates in place** instead of duplicating.

Columns include the required `bookmakers, market, event_date, roi_pct, max_liquidity` plus
`detected_at_utc, status, signature, actionable, match, fixture_id, tournament, kickoff_utc,
market_id, market_type, period, line, legs_json, arb_sum_S, roi_decimal, total_stake_max,
stake_split_json, max_profit, binding_book, min_leg_limit, shadow_books, involves_exchange,
low_confidence, suspicious, bet_links_json`.

---

## Setup

### 1. Secrets (GitHub → Settings → Secrets and variables → Actions)

| Secret | Purpose |
|---|---|
| `ODDS_PAPI_KEY` | OddsPapi API key (sent as `apiKey` query param) |
| `TELEGRAM_BOT_KEY` | Telegram bot token |
| `TELEGRAM_GROUP_ID` | chat_id of the group to post to |

Secrets are read from the environment only — never hardcoded or printed.

### 2. First run

1. Run the **`refresh-catalog`** workflow once (manually). It caches
   `data/cache/{sports,bookmakers,markets,tournaments}.json` (~4 requests) and **prints the
   resolved friendlies tournament IDs** — pin them in `config.yaml` under
   `tournaments.pinned_ids` so future scans skip discovery.
   > If catalogs are missing, `arb-scan` will refresh them inline once (budget permitting).
2. Run **`arb-scan`** via *Run workflow* with `dry_run: true` to verify the logs show the
   budget line, matched fixtures, and full arb calculations, and that the CSV is written.
3. Flip `dry_run` off (and/or let the 15-minute schedule take over) to start sending Telegram alerts.

### Local development

```bash
pip install -r requirements.txt
pytest                      # math, stake sizing, clone dedup, classification, line matching, CSV dedup
ODDS_PAPI_KEY=... TELEGRAM_BOT_KEY=... TELEGRAM_GROUP_ID=... DRY_RUN=1 python -m src.run
```

---

## Configuration (`config.yaml`)

Key knobs (see the file for all defaults):

- `target_window.{from_utc,to_utc}` — the UTC date window to scan.
- `tournaments.{match_name_regex,pinned_ids,national_teams_only}` — friendlies resolution.
- `bookmakers.{actionable,tracked,exchanges,commission}` — funded vs tracked books, exchange fees.
- `markets.{exclude,allow_quarter_lines}` — Double Chance excluded; quarter lines off by default.
- `thresholds.*` — ROI floor/ceiling, min stake, staleness, dedup window.
- `telegram.{rank_by,send_when_empty,local_tz}` — ranking (`profit` = ROI·T_max, or `roi`).
- `budget.{safety_margin,names_cache_hours}` — quota guard + name-cache TTL.
- `api.{odds_verbosity,odds_format}` — bump verbosity if markets are missing.

---

## Request budget (free tier = 250 / month)

- **Pre-flight guard:** every run calls free `/v4/account` first; if
  `request_limit − request_count ≤ safety_margin` (15) it logs a warning and **exits 0**
  (no billable calls, build not failed).
- **Static catalogs are cached** in `data/cache/` and refreshed by the separate
  `refresh-catalog` workflow only — not on every scan.
- **Each scan costs ~`max_books_per_cycle` requests** (one `odds-by-tournaments` call per book;
  default 4). The fixtures name map costs 1 request **per pinned tournament**, at most once every
  `names_cache_hours` (6) — so 2 pinned tournaments = 2 extra requests on a refresh cycle.
- A run can never overspend: the fetch loop stops as soon as the per-run budget would hit the
  safety margin, and the pre-flight guard skips the whole scan when the monthly quota is low.
- **Cadence vs. longevity:** the budget guard prevents *overspend*, but cadence controls how long
  250 requests last. At 4 books/cycle, the default 3-hour cron ≈ 32 req/day. Raise the cron
  frequency for a live burst, or `max_books_per_cycle` for more coverage — both cost more/month.
  *Upgrading the OddsPapi plan is what unlocks more books per call / a higher quota.*
- Cooldowns are respected (1000 ms; 2000 ms for `/v4/fixtures`). A 429 stops the run immediately.

### Future upgrade

OddsPapi offers a **WebSocket feed** for true real-time odds. After upgrading the plan, that is
the path to live/in-play arbing; this repo's polling design is the pre-match foundation. Point
`tournaments.pinned_ids` at the World Cup tournament when scope expands.

---

## Risk caveats

Realising an arb depends on **execution speed** (lines move between placing legs), **bookmaker
stake limits and bet voids/cancellations**, **exchange liquidity and commission**, and the
**availability of each book in your jurisdiction** (Ontario, Canada). The bot surfaces
opportunities and recommended stakes; **you place and verify the bets.** Treat any arb above the
suspicious ROI threshold, and any low-confidence / exchange-liquidity arb, with extra caution.

---

## Repo layout

```
soccer-papi/
├─ .github/workflows/
│  ├─ arb-scan.yml          # scheduled + manual scanner; commits CSV back to main
│  └─ refresh-catalog.yml   # weekly + manual catalog refresh
├─ src/
│  ├─ config.py             # config.yaml + env secrets / dispatch inputs
│  ├─ oddspapi.py           # API client: throttling, retries, 429-stop, budget guard
│  ├─ catalog.py            # cached catalogs, MECE market specs, clone map, friendlies IDs, names
│  ├─ normalize.py          # odds payload → per-fixture/market/outcome candidate table
│  ├─ arbitrage.py          # arb math, clone-aware leg selection, stake sizing, signatures
│  ├─ csv_store.py          # append/dedup to data/arbitrage_opportunities.csv
│  ├─ telegram.py           # format + send top 3
│  ├─ run.py                # one scan cycle (entrypoint: python -m src.run)
│  └─ refresh.py            # catalog refresh (entrypoint: python -m src.refresh)
├─ tests/                   # pytest: math, stake sizing, clone dedup, classification, CSV dedup
├─ data/
│  ├─ arbitrage_opportunities.csv   # created on first run
│  └─ cache/                        # committed cached catalogs
├─ config.yaml
└─ requirements.txt
```
