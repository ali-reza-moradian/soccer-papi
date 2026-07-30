# AUDIT CLOSEOUT — reconcile of `AUDIT_REPORT.md` against HEAD

**Audit snapshot:** 2026-07-29 ~19:11Z (see [AUDIT_REPORT.md](AUDIT_REPORT.md)).
**Reconciled against:** `8ecc400` (2026-07-30).
**Method:** every ID re-verified **by reading the code at HEAD**, not by line number — the audit's line
references have shifted (`pregame_exec.py` grew to 2,974 lines across the post-audit commits). A row
says FIXED only when the mechanism the finding describes is absent from HEAD.

> **Reading order.** This file is the reconcile. `PHASE 1` in the status column means "OPEN at
> `8ecc400`, scheduled for closure in the execution-integrity closeout that follows this commit" —
> those rows are rewritten to `FIXED@<sha>` when the work lands, so this document is never ahead of
> the code.

**Status vocabulary**

| Mark | Meaning |
|------|---------|
| `FIXED@<sha>` | The mechanism is gone at HEAD. Commit named. |
| `PARTIAL@<sha>` | The dangerous half is gone; a named residual remains. |
| `OPEN` | Present at HEAD, not scheduled in phase 1. |
| `OPEN → PHASE 1` | Present at HEAD, scheduled in this closeout. |
| `N/A` | Not a code defect (an economics observation, or a premise the audit itself refuted). |

**Phase 1 scope:** correctness P0/P1s that gate live trading. Speed (F10–F11), capacity/markets
(F12–F13, N14–N15), reporting (F2, N27–N28) and hygiene (N33–N36) are deliberately **out** of phase 1
and stay OPEN — they are listed with their own rows so nothing is lost.

---

## 0. Counts at `8ecc400`

| | FIXED | PARTIAL | OPEN | of which → PHASE 1 | N/A | total |
|---|---|---|---|---|---|---|
| F1–F13 | 1 | 4 | 8 | 3 | 0 | 13 |
| N1–N36 | 13 | 0 | 22 | 12 | 1 | 36 |
| **all** | **14** | **4** | **30** | **15** | **1** | **49** |

Pre-existing closures (14) all landed in the four post-audit commits `aff9409`, `6c3202a`, `acb36a2`,
`594aeb3`/`8ecc400`, plus the earlier `0b52747` / `8acfe0a` work the audit already accounted for.

After phase 1 the OPEN count drops to 15, **none P0**, one P1 (F2, reporting-only).

---

## 1. F1–F13

| ID | Sev | Status | Verified at HEAD |
|----|-----|--------|------------------|
| **F1** settlement sweep silence | P1 | **OPEN → PHASE 1** | `reconcile_settlements` reads `_market_legs`, calls the reconciler, prunes — and has no age signal of any kind. Read end to end: nothing anywhere compares an expected leg's `ts` against now. ZHELAN could cycle it every 15 min for 2.5 days in silence. |
| **F2** summary vs caps counters | P1 | **OPEN** | Top-level `summary()["fills"]`/`["pnl_today"]` are still `MakerState.n_fills`/`.pnl_today`, reset per restart (`state._roll` keeps only the lifetime tuple). Mitigating: the digest line **already** reads `caps.fills_today`/`caps.pnl_today`/`caps.stake_today`, and `_live_ctx` uses caps — so the *alerts* are truthful and only the panel/heartbeat top line is since-restart. Reporting-only; no rail reads it. Deferred. |
| **F3** UTC day roll | P1 | **PARTIAL@acb36a2** | The dangerous half — a safety halt evaporating at 00:00Z — is fixed: `LiveCaps.STICKY_HALTS = ("orphan_position", "booking_quarantine")` and `roll()` returns before clearing `halted`/`halt_reason` for those. Residual OPEN: no config knob to roll at a quiet hour; the daily budget still resets at 00:00Z mid-US-evening. That is a policy choice, not a defect. |
| **F4** untracked bucket vs the $50 rail | P1 | **OPEN → PHASE 1** | `_market_legs` records carry no booked estimate and no booking day, and `reconcile_settlements` never touches `caps`. So a settled row for a market booked today cannot restate `pnl_today`. (`caps.adjust_pnl` exists — `aff9409` — but only `settle_provisional_marks` calls it, i.e. only the naked-mark bucket.) |
| **F5** duplicate fills / WS-REST race | **P0** | **OPEN → PHASE 1** | Same finding as N3 — see there. |
| **F6** BOM + persistence robustness | P1 | **OPEN → PHASE 1** | Exactly one `utf-8-sig` reader exists (`_load_daily_caps`, `6c3202a`). `_load_orphan`, `_load_traded_tokens`, `_load_expected_positions`, `_load_settled_ledger`, `_load_provisional`, `state.load_tuning` and `state.bump_restart` all read plain `utf-8`, and the first four swallow every error. Four persisters (`persist_daily_caps`, `_persist_traded_tokens`, `_persist_expected_positions`, `_persist_settled_ledger`) `except: pass`. Seven persisters use a fixed `path + ".tmp"`. |
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
| **N3** fill-sweep ADD + no cross-detector dedupe | **OPEN → PHASE 1** | All three mechanisms present: `poll_kalshi_fills` routes `lo.matched_seen + cnt` (an ADD); `on_kalshi_fill` routes `lo.matched_seen + count` (an ADD, with no id dedupe at all); `parsing.parse_kalshi`'s `fill` branch carries no `trade_id`; `_seen_fill_ids` is written only by the sweep. Nothing clamps a cumulative to `lo.size`. |
| **N4** cumulative-vs-delta complement | **OPEN → PHASE 1** | `_hedge_fill` reads the **cumulative** venue complement (`_complement_shares(..., lo.matched_seen)`) and then uses it as *this fill's* hedge: `hedged = max(hedged, complement)` → `remainder = max(0, matched − hedged)` computes 0 on a second fill, and `_note_expected_legs` registers the cumulative as an increment. |
| **N5** in-play circuit not persisted | **OPEN → PHASE 1** | `inplay_halted`, `inplay_pause_until`, `inplay_fills_today` are plain attrs; `_load_daily_caps` restores only stake/fills/pnl. 11–21 restarts/day re-arm a tripped in-play halt. |
| **N6** `on_fill` had no plausibility ceiling | **FIXED@acb36a2** | `LiveCaps.implausible_fill_pnl` / `fill_pnl_bound` — two regimes (a hedged pair bounded by `ceiling × pair_cap`, a realized unwind by `2 × pair_cap`); an implausible pnl books **zero** and halts `implausible_fill_pnl`. |

### P1

| ID | Status | Verified at HEAD |
|----|--------|------------------|
| **N7** two concurrent live makers | **OPEN → PHASE 1** | No pid-lock, no cmdline probe anywhere in the maker's startup. `scripts/ops.py` `missing_components` matches only the wrapper substring `run_maker_rt_loop.ps1`. |
| **N8** REFUSED settlements are a black hole | **FIXED@acb36a2** | `SettledPnlReconciler._queue_refused` persists every refused row to `maker_rt_REFUSED_SETTLEMENTS.json` and deliberately does **not** mark the key settled, so a repaired cost basis can still reconcile it. |
| **N9** cancel swallows a raced fill | **OPEN → PHASE 1** | `_venue_order_state` tests `status in (CANCELED, CANCELLED)` **before** the matched-delta branch, so a partially-filled-then-cancelled order returns `"canceled"` and `_cancel` pops it, discarding the delta — the opposite priority to `poll_open_orders`. |
| **N10** `matched_seen` advanced before the hedge | **OPEN → PHASE 1** | `_on_fill_detected` sets `lo.matched_seen = total_matched` and *then* calls `_hedge_fill` inside the same `try`, with only `finally: release`. An exception consumes the delta permanently. `state._append_csv` opens unguarded. `_BaseFeed.run` catches every callback exception as "socket error". |
| **N11** Poly provisional marks never rebooked | **OPEN** | `_venue_realized` returns `None` for any non-Kalshi venue, so a Poly-side worst-case mark is never restated. The *re-orphan trap* half **is** closed (`_forget_settled_instruments` + `_forget_instrument`). Deferred. |
| **N12** startup stray-cancel fails open | **PARTIAL@acb36a2** | The leak the audit measured — a SHADOW process abandoning a prior LIVE run's resting orders (six Kalshi quotes, −$38.08 unhedged) — is fixed: `__main__` sweeps when `pregame_exec is None` too and screams. Residual OPEN: the sweep still does not **re-list** afterwards, so arming with a surviving `mrt-` order is possible in principle. |
| **N13** tree write not atomic | **OPEN → PHASE 1** | `write_tree` is a plain truncate-write of both files (`save_series_map` 12 lines away already does tmp + `os.replace`). `load_tree` raises `JSONDecodeError` straight into `load_trees` → `build_universe` → the loop. |
| **N14** stake cap is a placement throttle | **OPEN** | `can_place` checks one projected pair; open-order pairs are never reserved. 12 × $350 committable. Markets roadmap, and gated behind re-measurement by the audit's own Money §4. |
| **N15** no per-game concentration cap | **OPEN** | No per-game open cap; sizing does not subtract a market's already-committed pair cost. Markets roadmap. |
| **N16** in-play caps block is inert | **OPEN → PHASE 1** | `pregame_exec.__init__` reads only `first_fill_pause_s`, `halt_locked_net`, `freeze_cooloff_s` off `live_inplay`; sizing uses the shared `caps`. `quote_usd_max: 5 / max_open_quotes: 1 / max_fills_per_day: 4 / max_daily_loss_usd: 20` are declared and unenforced — and `quote_usd_max`'s comment ("match pre-game") is false, pre-game is 70. |
| **N17** hedged edge ≈ zero | **N/A** | An economics finding, not a defect. The restatement makes the number legible: hedged lifetime **+$15.02**, untracked **+$5.00**, 23 settled trades. Re-measurement gate stands (audit Money §4). |

### P2/P3

| ID | Status | Verified at HEAD |
|----|--------|------------------|
| **N18** cancel-retry storm | OPEN | `_cancel` retries a FILLED-but-uncancellable order every tick with a sync DELETE+GET, unthrottled. |
| **N19** reconcile is surplus-only | OPEN | `reconcile_positions` flags only `unexplained > 0.5`; an under-held expected leg is invisible until settlement. |
| **N20** restart queue loss | OPEN | Every exit cancels all resting orders; `_find_resting` re-adopt is unused. |
| **N21** terminal-"closed" substring match | **OPEN → PHASE 1** | `_on_place_failed` still does `any(s in msg_l for s in (..., "not_active", "closed"))` — a transport `"Remote end closed connection"` blacklists a live candidate for 24h. |
| **N22** sweep low-water advanced on a venue error | **OPEN → PHASE 1** | `KalshiOrderClient.fills_since` returns `[]` on a read failure — indistinguishable from "no fills" — and `poll_kalshi_fills` advances `_last_fills_sweep_ts` unconditionally. Fills in the gap are permanently unseen. |
| **N23** in-play circuit skips `locked=None` | **OPEN → PHASE 1** | `_apply_inplay_circuit` gates on `result["locked_net"] is not None`; a decline/unwind's realized loss never reaches the −2% circuit. |
| **N24** partial-unwind realized cost discarded | **OPEN → PHASE 1** | The not-ok branch of `_unwind_and_record` books `fill_price × remaining` and ignores `u["cost"]` — the money actually spent on the part that DID sell. |
| **N25** Kalshi seq-gap resync is verify-blind | OPEN | `_subscribe` fires and clears the resync flag without waiting for a `subscribed` ack; `kalshi_error` frames still have no consumer. |
| **N26** unwind cost excludes the exit taker fee | **OPEN → PHASE 1** | Both `_verified_unwind_*` compute `cost = (fill_price − sell_px) × sold` with no fee term. |
| **N27** achievable ladders volatile | OPEN | Reservoirs reset at midnight and on every restart; not persisted. |
| **N28** `quote_age_s` empty on live fill rows | OPEN | `_record_fill` writes no `quote_age_s`. |
| **N29** panel http server roots at `data\` | OPEN | Unverified network exposure; no code change. |
| **N30** sizing race on daily headroom | OPEN | `plan_size` gets the full remaining headroom per concurrent quote; the legacy `size_shares` min-clamp is still present (unused by the live path). |
| **N31** 1-cent avg misparse | **FIXED@acb36a2** | `c >= 1.0` boundary. |
| **N32** `int(round())` over-buy; `int(n)` truncation; WS `count_fp` | **OPEN → PHASE 1 (partial)** | The WS-parser half is in phase-1 scope (it sits on the exact line N3 touches). `hedge.py`'s `int(round())` and `_normalize_order_response`'s `filled = int(filled_f)` truncation stay deferred. |
| **N33** stale docstrings / dead code / silent unknown config keys | OPEN | `InplayLiveExecutor` is dead code carrying the pre-fix unwind pattern; `on_loss` and `head_poll_s` are dead. Phase 1 adds only a *targeted* unknown-key warning (see N16). |
| **N34** smoke Telegram bypasses HTML escape; fixed `.tmp` names; `at_best` counts unreadable views | **OPEN → PHASE 1 (partial)** | The fixed-`.tmp` half is F6's. `smoke.py`'s unescaped sender and `sample_metrics` counting an unreadable Poly view as an at-best hit stay deferred. |
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
| Q5 Do WS `fill` frames carry `count` or only `count_fp`? | Made moot by phase 1: the parser will read both, and the WS path stops booking off the frame's count at all. |
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
