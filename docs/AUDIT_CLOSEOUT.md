# AUDIT CLOSEOUT — reconcile of `AUDIT_REPORT.md` against HEAD

**Audit snapshot:** 2026-07-29 ~19:11Z (see [AUDIT_REPORT.md](AUDIT_REPORT.md)).
**Reconciled against:** `8ecc400` (2026-07-30), then closed out by **PHASE 1**.
**Method:** every ID re-verified **by reading the code at HEAD**, not by line number — the audit's line
references had shifted (`pregame_exec.py` had grown to 2,974 lines across the post-audit commits). A row
says FIXED only when the mechanism the finding describes is absent from the code.

> **`PHASE1` = the commit this file ships in** — `audit phase 1: execution integrity closeout`. The
> reconcile was committed FIRST, with those rows marked `OPEN → PHASE 1`, so this document was never
> ahead of the code at any point; they were rewritten to `FIXED@PHASE1` when the work landed.

**Status vocabulary**

| Mark | Meaning |
|------|---------|
| `FIXED@<sha>` | The mechanism is gone. Commit named. |
| `PARTIAL@<sha>` | The dangerous half is gone; a named residual remains. |
| `OPEN` | Still present, deliberately not in phase 1. |
| `N/A` | Not a code defect (an economics observation, or a premise the audit itself refuted). |

**Phase 1 scope:** correctness P0/P1s that gate live trading. Speed (F10–F11), capacity/markets
(F12–F13, N14–N15), reporting (F2, N27–N28) and hygiene (N33–N36) are deliberately **out** of phase 1
and stay OPEN — they are listed with their own rows so nothing is lost.

---

## 0. Counts

| | FIXED | PARTIAL | OPEN | N/A | total |
|---|---|---|---|---|---|
| F1–F13 | 4 | 4 | 5 | 0 | 13 |
| N1–N36 | 17 | 2 | 16 | 1 | 36 |
| **all** | **21** | **6** | **21** | **1** | **49** |

At `8ecc400` this read 14 FIXED / 4 PARTIAL / 30 OPEN. Phase 1 closed **16 rows outright** (F1, F4, F5,
F6, N3, N4, N5, N7, N9, N10, N13, N21, N22, N23, N24, N26), **resolved N16 by decision**, and closed the
named half of **N32** and **N34**.

Of the 21 still OPEN: **none is a P0**, and the single P1 (F2) is reporting-only — no rail reads it.
Every P0 in the report is closed.

Pre-existing closures landed in `aff9409`, `6c3202a`, `acb36a2` and `594aeb3`/`8ecc400`, plus the
earlier `0b52747` / `8acfe0a` work the audit already accounted for.

**Regression coverage:** `tests/test_maker_rt_audit_phase1.py` — 48 tests, one or more per finding, each
built on the incident's own numbers (order `329a9a89`, PHIMIA, CSKA's 318-lot, ZHELAN, FORBOT). Suite:
1,164 passing. The three P0 fixes were mutation-checked before shipping: reverting the shared fill-id
set, the complement increment, or the cancel-path delta check each fails its test.

---

## 1. F1–F13

| ID | Sev | Status | Verified at HEAD |
|----|-----|--------|------------------|
| **F1** settlement sweep silence | P1 | **FIXED@PHASE1** | `reconcile_settlements` now runs `_settlement_age_watchdog` on **every** pass, whether or not anything settled — a reconciler that only speaks when it succeeds cannot report the one failure that matters. Any EXPECTED leg older than `SETTLE_AGE_ALERT_S` (24h) -> ERROR log + Telegram, throttled to **1/day/pair** via `_settle_age_alerted`, **and** the venue position is re-read so the deficit itself is reported (a leg the venue no longer shows is a missed payout; a leg still held is genuinely awaiting settlement). `since_ts` is stamped when a leg is FIRST booked and never refreshed by a later fill, so a second fill cannot reset the clock. |
| **F2** summary vs caps counters | P1 | **OPEN** | Top-level `summary()["fills"]`/`["pnl_today"]` are still `MakerState.n_fills`/`.pnl_today`, reset per restart (`state._roll` keeps only the lifetime tuple). Mitigating: the digest line **already** reads `caps.fills_today`/`caps.pnl_today`/`caps.stake_today`, and `_live_ctx` uses caps — so the *alerts* are truthful and only the panel/heartbeat top line is since-restart. Reporting-only; no rail reads it. Deferred. |
| **F3** UTC day roll | P1 | **PARTIAL@acb36a2** | The dangerous half — a safety halt evaporating at 00:00Z — is fixed: `LiveCaps.STICKY_HALTS = ("orphan_position", "booking_quarantine")` and `roll()` returns before clearing `halted`/`halt_reason` for those. Residual OPEN: no config knob to roll at a quiet hour; the daily budget still resets at 00:00Z mid-US-evening. That is a policy choice, not a defect. |
| **F4** untracked bucket vs the $50 rail | P1 | **FIXED@PHASE1** | `_market_legs` records now carry `booked_day` + `booked_pnl` (accumulated by `_note_pair_legs` from the fill-time estimate), and `_restate_same_day` applies `sum(settled realized) - booked estimate` through `caps.adjust_pnl`, summed across **all** buckets for that market (hedged row + untracked-excess rows). Three guards make it safe on live money: once per market ever (`restated`), only markets first booked **today** (yesterday's rail is closed and already reported), and skipped when either leg still carries a provisional mark — `settle_provisional_marks` owns that correction and computes it from the same venue fills, and two owners of one correction is a double-count. |
| **F5** duplicate fills / WS-REST race | **P0** | **FIXED@PHASE1** | See N3 — the same finding. No detector adds a delta any more, one shared fill-id set, and `matched_seen` is a high-water mark of venue truth. |
| **F6** BOM + persistence robustness | P1 | **FIXED@PHASE1** | All seven loaders go through `_load_json`: `utf-8-sig` always, and a scream instead of `except: pass`. `_load_orphan` and `_load_traded_tokens` **FAIL CLOSED** — an unparseable file halts with `halt_reason='unreadable_state'` (added to `STICKY_HALTS`, so midnight cannot clear it) rather than dropping the freeze or blinding reconciliation. `state.load_tuning` renames an unreadable file to `.unreadable.bak` instead of letting the next persist overwrite lifetime pnl with zeros. Every persister goes through `_persist_json` -> `state.atomic_json` (per-pid tmp, retry, `assert_writable`, truthful bool) and raises an ERROR + a 15-min-throttled Telegram on failure. The seven fixed-`.tmp` names are gone. Verified against the live files before deploying — an unparseable one of the two fail-closed files would have halted the bot on restart. |
| **F7** place-fail skip list not persisted | P2 | **OPEN** | `_place_fail_until` / `_place_fail_n` are plain dicts cleared by `roll_day` and absent from the startup load set. Every restart re-POSTs closed markets once. Deferred (P2 — the terminal backoff still works within a process). |
| **F8** test pollution of `executor.log` | P2 | **OPEN** | `src/executor/cli.py::_logger` still attaches an unguarded `FileHandler(exec_config.LOG_PATH)`; `tests/conftest.py` patches only maker_rt's `GENZ_DIR`/`OPS_DIR`. Blindness only; maker_rt state is protected by the two-layer guard. Deferred. |
| **F9** orphan classes + precision | P1 | **PARTIAL@acb36a2** | Precision hole 1 **fixed**: `_avg_price_cents_to_dollars` boundary is now `c >= 1.0`, so a 1-cent unwind fill no longer books as $1.00. Residual OPEN: `hedge.py` still sizes with `int(round(...))` (over-buys ≤0.5 sh — N32); bounded auto-flatten still reaches only class (c), and the class-(b) reconciliation path still uses the legacy `auto_flatten` bool that halts even on success. |
| **F10** sync I/O on the async loop | P1 | **OPEN** | Still one `await` in the loop; zero `to_thread`. Attribution *was* added (`blockers` map in `__main__`, `f173721`/`a7ff472`) so the stall is measured and named per subsystem, but nothing has moved off the loop. Speed roadmap. |
| **F11** papermaker dominates the cycle | P1 | **OPEN** | `papermaker.py` still calls `_best_bid` before the unchanged-quote short-circuit. `6316b89`/`1bce816` corrected the *attribution* (the 85% is the papermaker, not the snapshot) but did not plumb `best_bid` through `PricedVenue`. Speed roadmap. |
| **F12** capacity + log bloat | P1 | **PARTIAL@2ba6b46** | The 300s `REFUSE_LOG_EVERY_S` throttle exists and suppressed hits are counted. Residual OPEN exactly as described: the CSV `expire` row at the slot-refusal site is still unthrottled (the 117k rows/day), and `refuse_suppressed` is still incremented and never read into the digest line. Markets roadmap. |
| **F13** no edge feedback loop | P1 | **OPEN** | `achievable_net` still lands only in measurement sinks; repo-wide grep finds no reader. No `maker_rt.sports:` per-sport live switch. Markets roadmap. |

---

## 2. N1–N36

### P0

| ID | Status | Verified at HEAD |
|----|--------|------------------|
| **N1** Kalshi NO-side price space | **FIXED@acb36a2** | `_normalize_order_response` takes `side` and decodes in three tiers of authority: **venue cash** (`taker_fill_cost_dollars + maker_fill_cost_dollars` ÷ fill count — exact across a multi-level sweep), then the **side-named** `no_price_dollars`/`yes_price_dollars`, then legacy `average_fill_price` converted `1−p` for NO. `place_order` threads `side` through. `LiveHedger._warn_if_diverged` screams at a ≥10¢ book-vs-executed gap, and `book_refuse_reason` refuses the booking outright (`pair_out_of_band` / `locked_above_ceiling` → quarantine + halt). Ledger restated (`data/ops/maker_rt_RESTATEMENT_20260729.json`): hedged lifetime +$10.16 → **+$15.02**, untracked $43.47 → $5.00. |
| **N2** midnight clears the orphan halt | **FIXED@acb36a2** | `LiveCaps.STICKY_HALTS` + the early return in `roll()`. |
| **N3** fill-sweep ADD + no cross-detector dedupe | **FIXED@PHASE1** | Four changes. (1) `parsing.parse_kalshi` carries `trade_id` and reads `count_fp` — it discarded both. (2) ONE `_seen_fill_ids` set, marked by the socket handler **and** the account sweep. (3) Neither adds: both route through `_route_venue_cumulative`, which re-reads the order's **cumulative** `fill_count` from the venue, so two detectors seeing one execution compute the same number and `_on_fill_detected` acts only on the increase. (4) The one remaining additive path — an unreadable per-order read — is bounded by `lo.size`, because our own arithmetic gets our own bound; a VENUE read above the order size is screamed about and hedged in full instead, since under-hedging is the worse error. Pinned: a partial fill seen by the socket then re-seen by the sweep hedges **exactly once**. |
| **N4** cumulative-vs-delta complement | **FIXED@PHASE1** | `_LiveOrder.hedged_seen` tracks the hedge shares already attributed to an order. `_hedge_fill` takes the cumulative explicitly (`cumulative=`) instead of reading `lo.matched_seen`, keeps the nakedness test cumulative-vs-cumulative, and computes THIS fill's hedge as `complement - hedged_seen`. So a second fill's remainder is `this fill's matched - this fill's hedge` and is genuinely unwound; expected legs register the increment, not the cumulative (they used to double-register). |
| **N5** in-play circuit not persisted | **FIXED@PHASE1** | `inplay_halted`, `inplay_fills_today` and the first-fill pause (stored as REMAINING seconds, not a wall-clock instant another process cannot interpret) ride in the day-keyed caps file and are restored same-day; `_load_daily_caps` also pins `self._day` so the first `roll_day` cannot 'reset' what it just read. Persisted immediately when the halt trips and when the pause starts. A genuine new UTC day still clears all three. |
| **N6** `on_fill` had no plausibility ceiling | **FIXED@acb36a2** | `LiveCaps.implausible_fill_pnl` / `fill_pnl_bound` — two regimes (a hedged pair bounded by `ceiling × pair_cap`, a realized unwind by `2 × pair_cap`); an implausible pnl books **zero** and halts `implausible_fill_pnl`. |

### P1

| ID | Status | Verified at HEAD |
|----|--------|------------------|
| **N7** two concurrent live makers | **FIXED@PHASE1** | `singleton.guard` takes an OS-held exclusive byte-range lock (`msvcrt.locking` on Windows / `fcntl.flock` elsewhere) on `data/ops/maker_rt.lock` at the top of `_run` — before the restart counter, before a client, before a socket. Not a pid file: the kernel releases it on process death, so there is no stale-lock trap and no 'is pid 4212 still this bot?' guess to get wrong. A second instance logs CRITICAL, alerts (throttled 15 min, because the wrapper relaunches every 5s), and exits 3 having placed and counted nothing. `scripts/ops.py` gains `Component.alt_matches`, and maker_rt now also matches a bare `-m src.genz.maker_rt`, so a dead wrapper with a surviving child no longer reads as 'missing'. |
| **N8** REFUSED settlements are a black hole | **FIXED@acb36a2** | `SettledPnlReconciler._queue_refused` persists every refused row to `maker_rt_REFUSED_SETTLEMENTS.json` and deliberately does **not** mark the key settled, so a repaired cost basis can still reconcile it. |
| **N9** cancel swallows a raced fill | **FIXED@PHASE1** | `_cancel_confirmed` checks the matched delta **first** — the priority `poll_open_orders` always had — and routes it to `_on_fill_detected` before anything frees the slot; only then is a terminal cancel honored. Where no book is available (shutdown / feed-down cancel-alls) it refuses to confirm, so the order stays tracked and the forced poll routes the fill. `_cancel` no longer double-decrements `caps.open_quotes` when the router already closed the order out, and `poll_open_orders` releases a CANCELED order regardless of `matched_seen` — which also closes a latent PERMANENT slot leak on a partially-filled-then-cancelled order. Cost measured before shipping: the extra venue read touches only the *confirmed*-cancel path (~107/hour peak = 0.03/s); the 1.36/s `cancel NOT confirmed` cases already did that read. |
| **N10** `matched_seen` advanced before the hedge | **FIXED@PHASE1** | The advance moved **after** `_hedge_fill` returns (locked, declined or unwound). An exception no longer consumes the delta: `_on_hedge_exception` logs CRITICAL with a traceback, alerts, leaves `matched_seen` untouched so the next detector retries — and that retry is safe because the chain proves nakedness against venue truth first, so a pair that already locked books LOCKED rather than hedging twice — and escalates to an ORPHAN halt after `MAX_HEDGE_ERRORS` consecutive failures on one order. The in-flight guard is released on every path. `_append_csv` retries a transient Windows lock and drops the row rather than raising into the chain. `_BaseFeed._dispatch` logs a raising WS callback CRITICAL (first always, then throttled) and KEEPS the socket, instead of reporting an application bug as a socket error and tearing the connection down as the remedy. |
| **N11** Poly provisional marks never rebooked | **OPEN** | `_venue_realized` returns `None` for any non-Kalshi venue, so a Poly-side worst-case mark is never restated. The *re-orphan trap* half **is** closed (`_forget_settled_instruments` + `_forget_instrument`). Deferred. |
| **N12** startup stray-cancel fails open | **PARTIAL@acb36a2** | The leak the audit measured — a SHADOW process abandoning a prior LIVE run's resting orders (six Kalshi quotes, −$38.08 unhedged) — is fixed: `__main__` sweeps when `pregame_exec is None` too and screams. Residual OPEN: the sweep still does not **re-list** afterwards, so arming with a surviving `mrt-` order is possible in principle. |
| **N13** tree write not atomic | **FIXED@PHASE1** | `write_tree` writes both files through `_atomic_write_json` (per-pid tmp + `os.replace`) — the pattern `save_series_map` already used a few hundred lines up. `load_tree` reads `utf-8-sig`; `load_trees(previous=...)` keeps the last good tree per sport on an unparseable read instead of propagating a JSONDecodeError into the event loop; and `__main__` refuses to adopt an **empty** rebuilt universe over a non-empty one, without committing the mtimes, so it retries next heartbeat. This is the 3-crashes-in-a-day / cancel-every-resting-order pattern. |
| **N14** stake cap is a placement throttle | **OPEN** | `can_place` checks one projected pair; open-order pairs are never reserved. 12 × $350 committable. Markets roadmap, and gated behind re-measurement by the audit's own Money §4. |
| **N15** no per-game concentration cap | **OPEN** | No per-game open cap; sizing does not subtract a market's already-committed pair cost. Markets roadmap. |
| **N16** in-play caps block is inert | **RESOLVED@PHASE1 — deleted, not enforced** | **Decision: delete the dead keys and document shared-caps governance.** Why: the config already declared that model for three of the four keys ('INFORMATIONAL — the SHARED live.* governs pre+inplay'), and the code has exactly one `LiveCaps`, one in-flight guard and one daily budget spanning both phases by design. Enforcing `max_open_quotes: 1 / max_fills_per_day: 4 / max_daily_loss_usd: 20 / quote_usd_max: 5` would have imposed a **materially tighter policy nobody chose**, on a live armed bot, inside a closeout commit — that is a decision for whoever owns the risk, not a side effect of an audit fix. So: the four keys are removed from the `live_inplay:` YAML block and are no longer read from YAML at all; the rule is stated in one place (`DEAD_INPLAY_CAP_KEYS`, the config comment, the `InplayLiveConfig` docstring, and the ARM banner, which now says the shared caps govern both phases); and re-adding any of them logs a loud **`NOT ENFORCED`** warning naming the shared value that actually applies. The dataclass fields survive only as the dead `InplayLiveExecutor`'s own surface (N33) and can never again be mistaken for a live rail. Net: no declared-but-unenforced risk limit exists anywhere. To make in-play genuinely stricter than pre-game, the caps have to become per-phase in code first. |
| **N17** hedged edge ≈ zero | **N/A** | An economics finding, not a defect. The restatement makes the number legible: hedged lifetime **+$15.02**, untracked **+$5.00**, 23 settled trades. Re-measurement gate stands (audit Money §4). |

### P2/P3

| ID | Status | Verified at HEAD |
|----|--------|------------------|
| **N18** cancel-retry storm | OPEN | `_cancel` retries a FILLED-but-uncancellable order every tick with a sync DELETE+GET, unthrottled. |
| **N19** reconcile is surplus-only | OPEN | `reconcile_positions` flags only `unexplained > 0.5`; an under-held expected leg is invisible until settlement. |
| **N20** restart queue loss | OPEN | Every exit cancels all resting orders; `_find_resting` re-adopt is unused. |
| **N21** terminal-"closed" substring match | **FIXED@PHASE1** | `_terminal_place_failure` decides terminality from what the VENUE said, not from English: an explicit error code (parsed from `code`/`error`/`message`) against `TERMINAL_PLACE_CODES`, or one of those full snake_case codes in the body. A transport failure is checked FIRST and can never be terminal (`TRANSPORT_HINTS`), because no network error is evidence about the state of a market. 'Remote end closed connection' no longer blacklists a live candidate for 24h. |
| **N22** sweep low-water advanced on a venue error | **FIXED@PHASE1** | `KalshiOrderClient.fills_since` returns **`None`** on a read failure and `[]` only when the account genuinely had no fills; `poll_kalshi_fills` returns without advancing `_last_fills_sweep_ts` on `None`, so the interval is re-read instead of being stepped over forever. |
| **N23** in-play circuit skips `locked=None` | **FIXED@PHASE1** | Every result dict carries `realized_net` (per share): `locked` for a hedged pair, `-cost/shares` for a decline or unwind, `(flatten - first-sweep cost)/shares` for an auto-flatten, `-est_loss/shares` for `unwind_FAILED`, and `locked` for dust — an in-play fill we declined and could not even close is not neutral. `_apply_inplay_circuit` reads it, so a no-hedge-book decline that market-unwound at -16% now trips the -2% day-halt and persists it. `None` remains only for `book_refused`, which already halts everything via the quarantine. |
| **N24** partial-unwind realized cost discarded | **FIXED@PHASE1** | The not-ok branch books `fill_price x naked_remainder` **plus** the realized `(entry - exit) x sold` of what did sell; the auto-flatten branch books `flatten_pnl - first_sweep_cost`. The provisional mark still covers only the still-open remainder, so the already-realized part is not rebooked at settlement. |
| **N25** Kalshi seq-gap resync is verify-blind | OPEN | `_subscribe` fires and clears the resync flag without waiting for a `subscribed` ack; `kalshi_error` frames still have no consumer. |
| **N26** unwind cost excludes the exit taker fee | **FIXED@PHASE1** | `_exit_fee` adds the taker fee to `cost` on both venues: Kalshi prefers the venue-reported `fee` and falls back to the official `ceil_to_cent(0.07*C*P*(1-P))`; Poly reports no fee field, so it uses `poly_fee_usd(sold, px, rate)`. Pure spread understated every exit by roughly the size of the edge the whole strategy is chasing. |
| **N27** achievable ladders volatile | OPEN | Reservoirs reset at midnight and on every restart; not persisted. |
| **N28** `quote_age_s` empty on live fill rows | OPEN | `_record_fill` writes no `quote_age_s`. |
| **N29** panel http server roots at `data\` | OPEN | Unverified network exposure; no code change. |
| **N30** sizing race on daily headroom | OPEN | `plan_size` gets the full remaining headroom per concurrent quote; the legacy `size_shares` min-clamp is still present (unused by the live path). |
| **N31** 1-cent avg misparse | **FIXED@acb36a2** | `c >= 1.0` boundary. |
| **N32** `int(round())` over-buy; `int(n)` truncation; WS `count_fp` | **PARTIAL@PHASE1** | The WS-parser half is **fixed**: `parse_kalshi` reads `count_fp` as well as `count`, so the accelerator is no longer a silent no-op against a v2 payload. Residual OPEN: `hedge.py` still `int(round())`s the hedge size (over-buys <=0.5 sh) and `_normalize_order_response` still truncates `filled = int(filled_f)` (conservative). |
| **N33** stale docstrings / dead code / silent unknown config keys | OPEN | `InplayLiveExecutor` is dead code carrying the pre-fix unwind pattern; `on_loss` and `head_poll_s` are dead. Phase 1 adds only a *targeted* unknown-key warning (see N16). |
| **N34** smoke Telegram bypasses HTML escape; fixed `.tmp` names; `at_best` counts unreadable views | **PARTIAL@PHASE1** | The fixed-`.tmp` half is **fixed** by F6 (one per-pid atomic writer for every persister). Residual OPEN: `smoke.py`'s sender still bypasses `html.escape`, and `sample_metrics` still counts an unreadable Poly view as an at-best hit. Adjacent fix shipped anyway: `format_event("error", ...)` silently DROPS its `detail`, so two alert call sites (a persist failure and the shadow stray-order sweep) were emitting a line with no content in it — both now use `"problem"`. |
| **N35** log lines carry no date; loop-`now` CSV stamps; double summary; CSV open per row | OPEN | Unchanged. |
| **N36** og_multi quota self-heals to zero; executor `.env` at import | OPEN | Unchanged. |

---

## 3. Audit open question **Q3** — answered

> *"Was the 13:30:07Z ARM-file removal a deliberate operator disarm after the (phantom) CERBVB −$49.91 alert? If so: that loss was ~99% fiction."*

**YES.** The 13:30:07Z removal of both ARM files was the **operator reacting to the CERBVB alert**. N1
subsequently proved that alert was **~99% phantom**: CERBVB was a 92-share rest-poly leg at $0.76
hedged with 92 Kalshi **NO** at a real $0.23, read in YES-space as $0.77 — booking a $1.53/share pair
and a **−$49.91** "HEDGED AT A LOSS". The restatement puts the real figure at **−$0.2206**
(`maker_rt_RESTATEMENT_20260729.json`). So the disarm was a correct operator response to an incorrect
number: the alert was doing its job, the arithmetic behind it was not.

Two consequences worth stating plainly:

1. The disarm itself was **not** free. An unarmed process kept no hedger, but the previous LIVE run's
   six Kalshi quotes stayed resting; they filled 14:22Z–17:18Z **unhedged** and settled to −$38.08 of
   unintended naked exposure. That is N12's leak, and it is why `__main__` now sweeps stray orders in
   SHADOW too and says so loudly.
2. It is the strongest single argument for phase 1's shape. Every rail downstream of the books trusts
   the books, so a booking-time invariant that refuses an impossible pair **before** it reaches
   `pnl_today` is worth more than any number of alerts about it afterwards.

### The other open questions, for the record

| Q | State |
|---|-------|
| Q1 Kalshi v2 `average_fill_price` YES-space for NO orders? | **Resolved by construction.** HEAD no longer relies on it: venue cash (`*_fill_cost_dollars`) is primary and unambiguous. The legacy field is converted for NO and used only for v1/mocks. |
| Q2 Actual Kalshi debits for FORBOT / TOTSYD / CERBVB | **Resolved** — restated from venue statements in `maker_rt_RESTATEMENT_20260729.json`. |
| Q4 Why did ZHELAN take ~3 days to settle? | Still unknown. F1's watchdog (phase 1) makes the *next* one visible within 24h instead of never. |
| Q5 Do WS `fill` frames carry `count` or only `count_fp`? | **Made moot.** The parser reads both, and the WS path no longer books off the frame's count at all — it marks the trade id and asks the venue for the cumulative (N3). |
| Q6 Duplicate subscribe / Poly `size_matched` batching | Open (N25; speed roadmap 3). |
| Q7 Poly WS flap behaviour | Open — uncharacterised. |
| Q8 Can the shared Poly funder hold balance on a token this maker trades? | Open. Mitigated: `_complement_shares` is only ever allowed to *prevent* an unwind, never to cause a naked position. |
| Q9 The two identical TOTSYD 6.3sh rows on order `66fddf65` | **Explained as the F5 echo class** — a full-fill echo re-observed by a second detector. Phase 1's shared fill-id set + cumulative clamp make that row shape unproducible. |
| Q10 Is TCP 8080 reachable off-host? | Open (N29) — needs a network probe, not a code read. |
| Q11 Does off-repo tooling read the heartbeat / papermaker CSVs? | Open — in-repo there are still no readers. |

---

## 4. Deferred out of phase 1 (nothing here is a P0)

**Roadmaps, unchanged and still ranked in the report:** speed 1–9 (F10, F11, N18, N20), markets 1–6
(F12, F13, N14, N15), money 4–7 — re-measure before scaling; the audit's ≈10/13/33 clean-fill gates
per sport stand, and phase 1 raises no cap.

**Individually deferred:** F2, F7, F8, F9-residual, N11, N12-residual, N18, N19, N20, N25, N27, N28,
N29, N30, N32-residual, N33, N34-residual, N35, N36.

**One regression phase 1 caused, found in production 15 minutes after the deploy and fixed:**

Polymarket sends `size_matched` as a **string**, and `_venue_order_state` compared it to a float —
`TypeError: '>' not supported between 'str' and 'float'`. The bug was as old as that field's read path but
**unreachable**: `_cancel_confirmed` honored the `canceled` fast path first, so a Poly order was never
asked about. N9 reordered exactly that, and the LIVE process died at 04:04:27Z (graceful shutdown ran,
all 10 resting orders cancelled cleanly, wrapper restarted it 12s later; one crash, no stranded orders,
no fills in flight). `_order_matched` now coerces at the single place the field is read, so no caller has
to remember — the same lesson as `fp_num`. Pinned by
`test_a_string_size_matched_cannot_crash_the_cancel_path`, mutation-verified to reproduce the TypeError.

**Two things phase 1 found that the audit did not, both now closed:**

1. A **permanent slot leak**: the branch that releases a venue-terminal order required `matched_seen == 0`,
   and so did age-out — so a partially-filled-then-cancelled order held one of the twelve slots forever.
   Invisible before N9, because the old cancel path popped that order (losing its fill) instead of keeping
   it. Fixing one exposed the other.
2. `alerts.format_event("error", …)` **silently drops its `detail`**, rendering `• ERROR · • ? · ?`. Two
   live call sites used it — a persist failure and the SHADOW stray-order sweep, i.e. the alert for the
   leak that cost −$38.08 on 2026-07-29. Both now use `"problem"`, which does render the text.

**One measurement worth carrying forward:** `cancel NOT confirmed` fires up to **4,909 times an hour**
(1.36/s), each with a synchronous DELETE + GET on the event loop. That is N18, still OPEN, and it is now
the largest single unaddressed contributor to the F10 loop stall.
