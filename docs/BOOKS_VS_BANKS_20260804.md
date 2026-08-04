# Books vs Banks — 2026-08-04

**Mission: the exchanges' cash is the truth.** Close the gap between what `maker_rt` CLAIMS and what
Kalshi and Polymarket HOLD, and stop alerts from claiming certainty the venue doesn't back.

Nothing here changes quoting size, caps, halts, hedging policy or any trading rail. The one behaviour
change that touches pricing is T5, and it makes a quote *more* conservative on three series by the exact
amount the venue was already charging us.

---

## The headline

Flat-to-flat since the 2026-07-31 baseline, the books said **+$8.53** while the exchanges moved
**+$3.30**. Every cent of that $5.23 gap is now accounted for:

| | $ |
|---|---:|
| Exchanges actually moved (baseline → 2026-08-04T00:00Z, both flat) | **+3.2988** |
| Books claimed | +8.5301 |
| — three unwinds paid in cash and never booked into lifetime | **−6.9900** |
| Books, corrected | **+1.5401** |
| Remaining difference | **+1.7587** |
| explained by: the baseline was snapped with a pair open (Kalshi at COST $70.00, Polymarket at MARK $27.91; it settled for $100.00) | +1.8561 |
| explained by: naked share dust an unwind could not sell, still unbooked | −0.0994 |
| unexplained | **+0.0020** (rounding on the recorded unwind costs) |

Both causes are now fixed: exits enter lifetime (T2), and a snapshot holding an open pair may no longer
be a baseline or fire a red alert (T3).

---

## T1 — Every cash movement, classified

Full table: **`data/ops/CASH_CLASSIFIED_20260804.txt`**. Window `2026-07-31T15:27:25Z → 2026-08-04T14:12Z`.
Sources: Kalshi `GET /portfolio/fills` + `/portfolio/settlements`, Polymarket data-api `/activity`.
Read-only — the probe placed nothing and wrote no runtime state.

| venue | class | n | $ |
|---|---|---:|---:|
| kalshi | fill_open | 22 | −381.5946 |
| kalshi | fill_close | 4 | +102.8800 |
| kalshi | fee | 14 | −9.3805 |
| kalshi | settlement | 11 | +479.6500 |
| poly | fill_open | 17 | −732.1347 |
| poly | settlement | 3 | +184.0730 |
| — | transfer / rebate / **unknown** | 0 | 0.0000 |

**Reconciliation — both venues to the cent:**

```
Kalshi   baseline  4519.7251 + classified  +191.5549 =  4711.2800   venue says  4711.2800   diff +0.0000
Poly     baseline  2618.9910 + classified  -548.0618 =  2070.9292   venue says  2070.9292   diff -0.0000
```

Nothing was left unclassified, and there were no deposits or withdrawals in the window.

### Two Kalshi mechanics this forced us to get right

Getting these wrong is what made the first pass miss by $666, so they are written down.

**1. Kalshi debits COLLATERAL on OPEN and credits proceeds on CLOSE.** A fill reported as
`action=sell side=no book_side=ask` while FLAT is an *open of NO*: cash out = `count × (1 − yes_price)`.
Proven twice over:

- `KXCLUBFTOTAL-26AUG04JEJBMU-6` fills of 14.91 + 44.74 + 76.00 sh at no-prices 0.68/0.68/0.61 = $86.9220,
  and the settlement row reports `no_total_cost_dollars: 86.922000` for exactly 135.65 NO.
- Snapshot `2026-08-04T00:00Z` cash 4681.2909 → `08:00Z` 4575.6300 = **−105.6609**, which is precisely the
  two open collaterals (18.4500 + 86.9220 = 105.3720) plus the six taker fees (0.2889).

So the cash flow of a fill depends on the position held at that instant; the table tracks net YES per
ticker and splits each fill into its closing and opening parts.

**2. A settlement row's `fee_cost` is that market's LIFETIME fill fees restated — not a second charge.**
`JEJBMU-2`'s settlement `fee_cost` of 0.288900 equals the sum of its six taker fill fees to the cent.
Counting it again would have double-booked **$9.38** of fees across this window alone.

### The three unbooked exits, against venue cash

| when | market | books recorded | venue cash | residual |
|---|---|---:|---:|---:|
| 2026-08-02T00:31:19Z | 26AUG01DIMCAL total_goals\|1.5 | −2.2000 | −2.3106 | −0.1106 |
| 2026-08-02T02:45:17Z | 26AUG01PORSEA team_total\|portland\|1.5 | −4.7500 | −4.7421 | +0.0079 |
| 2026-08-02T22:00:43Z | 26AUG02RIVCARC total_goals\|5.5 | −0.0400 | −0.0367 | +0.0033 |
| | **TOTAL** | **−6.9900** | **−7.0894** | **−0.0994** |

The residual is **not** exit cost. DIMCAL bought 116.72 YES and Kalshi would only take 116.00 back
(whole-lot sells), so 0.72 sh × $0.16 = $0.1152 of cost stayed naked and expired worthless. The other two
are rounding on the recorded figure. **The restatement books the −$6.99 the books themselves recorded;
the −$0.0994 of naked dust is reported here and deliberately left unbooked** — see *Open items*.

---

## T2 — Exit costs now enter lifetime

**The bug.** `state.record()` sent `hedge_declined | hedge_unwound | unwind_FAILED` only to
`unwind_cost_today`, and only `trade_settled` / `mark_corrected` touched `settled_pnl_lifetime`. An
unwound market settles with us **flat**, so no settled row is ever written for it — and
`unwind_cost_today` rolls to zero at UTC midnight. The paid exit toll simply vanished.

**The fix.** A new lifetime counter `settled_pnl_exits_lifetime`, and on a VERIFIED exit the signed cost
goes into **both** `settled_pnl_lifetime` (so lifetime tracks venue cash) and the exits bucket (so the
hedged number stays pure).

Which events book, and why the third does not:

| event | booked? | why |
|---|---|---|
| `hedge_unwound` | always | `_verified_unwind` confirmed flat. A fully-hedged or dust row carries cost 0.0 and adds nothing. |
| `hedge_declined` | only with a real paid cost | Same verified-flat path. The zero-cost variants are a fill that needed no exit and un-closable dust; neither moved cash. |
| `unwind_FAILED` | **never** | That position still exists. It is marked provisionally and its real outcome arrives later as `mark_corrected` / `trade_settled`, which already reach lifetime. Booking it here too would count the same shares twice. |

**The formula is now in one place** — `state.hedged_lifetime(life, untracked, exits)`:

```
hedged = lifetime − untracked − exits
```

Exits are negative, so subtracting them makes hedged *larger* than lifetime. It is used by `state.summary`,
`state.heartbeat`, `balance.books_from` and `balance.run_cli`; there is no hand-spelled copy left.

**Idempotency.** An exit persists its counters immediately (`persist_tuning()`, the same rule
`hedge_locked` uses) and a fresh process reads them back. Nothing replays the CSV at startup, so there is
exactly one booking per exit regardless of how many times the process comes up. Test:
`test_a_deploy_restart_does_not_double_book_the_toll` runs three back-to-back restarts.

**The one-time restatement.** `data/ops/maker_rt_RESTATEMENTS.json`, key `exits-20260804`, booking
`exits_usd: -6.99` / `exits_n: 3`. Applied by `MakerState.apply_restatements()` immediately after
`load_tuning()` at startup. The key is written into the tuning file **by the same atomic write** that
moves the money, so there is no ordering in which one lands without the other; an already-listed key is
refused forever. Pre-Jul-30 history is untouched — `RESTATEMENT_20260729` still anchors it.

### New lifetime numbers

| | before | after |
|---|---:|---:|
| `settled_pnl_lifetime` | +32.3847 | **+25.3947** |
| `settled_pnl_hedged_lifetime` | +24.0059 | **+24.0059** (unchanged — this is the point) |
| `settled_pnl_untracked_lifetime` | +8.3788 | +8.3788 |
| `settled_pnl_exits_lifetime` | — | **−6.9900** |
| `settled_exits` | — | 3 |

Booking the toll pulls lifetime down to what the venues actually did **without moving the hedged edge**,
because the toll was never edge in the first place.

---

## T3 — Honest windows

`balance.py` stays a pure module; all three rules are unit-tested against synthetic snapshots.

**1. Only a FLAT snapshot may be a baseline.** A snapshot holding an open pair values Kalshi at COST
(`market_exposure` — the venue publishes no mark) and Polymarket at MARK (`currentValue`). Its total is
part cost and part mark, and no later total can honestly be subtracted from it. The 2026-07-31 baseline
was taken exactly that way and injected **$1.86** of pure valuation artefact into every lifetime
comparison after it. The first flat, ok snapshot after this deploys becomes `baseline_v2`; the old one is
kept as `baseline_v1` (the log is evidence — a bad measurement is superseded, never deleted) and the
report says so once.

Flatness asks whether a *valuation can move*, not whether anything is held: a resolved-but-unredeemed leg
is marked at $1.00 face and is worth exactly what it will convert to, so it stays "flat".

**2. A window with a pair open at EITHER endpoint may not fire 🔴.** It is labelled
`⚪ unreliable while pairs are open (marks move)` and judgement is deferred to the next flat-to-flat
window. The number is still measured and still printed — hiding it would be its own dishonesty. This is
what the ±$19–25 red MISMATCH alerts of Aug 1–2 were: nothing was wrong, the two sides simply were not
measured on the same basis. Flat-to-flat windows keep the $5 threshold unchanged, and a real breach now
says *"Both ends of this window were flat, so it is not mark noise."*

**3. Every window names the split.** A new line under each window and each breach:

```
   where: Kalshi cash -$10.00/positions +$0.00 · Polymarket cash -$15.00/positions +$0.00
```

It is printed always, not only on a breach — an operator who reads it only in a panic has not learned to
read it.

**Also fixed here:** `python -m src.genz.maker_rt --balance` raised `UnicodeEncodeError` on a Windows
cp1252 console *after* the venue reads, the persist and the re-anchor had happened — the audit ran,
changed state, and printed a traceback instead of its answer.

---

## T4 — The words match the position

All in `alerts.py`; escaping is unchanged (the sender HTML-escapes, so these stay plain text).

**1. An unhedged remainder forbids "GUARANTEED".** `26AUG04JEJBMU O/U5.5` bought 142.2222 Polymarket
shares against 135.65 on Kalshi. The pair made **+$1.80** and the 6.5722-share remainder lost **−$2.27
(−100%)** — the trade *lost money* while the alert announced it as a lock. Now:

> ✅ LOCKED · ⚽ Soccer · Jeju SK FC vs Bayern Munich · hedged 135.65 sh · 6.5722 sh riding unhedged
> ($2.27 at risk, settles on its own)
> …
> ⚠️ that net covers the 135.65 sh that are matched. The extra 6.5722 sh is a one-way bet I could not
> pair off — it wins or loses on its own.

The remainder is priced on whichever leg actually holds it (over-bought hedge → hedge price;
under-hedged fill → rest price). A 0.01-share tolerance keeps the warning off ordinary fractional dust,
and `→ pays` now names the matched shares rather than the whole fill.

**2. A dust fill with no hedge never uses the LOCKED template.** The 1-share U1.5 fill at 3¢ was
announced *"profit is GUARANTEED either way · pays $1.00"* with the hedge rendered `$— @ —` — a message
that showed no hedge and promised a certainty in the same breath. It now gets its own short line, honest
about both outcomes. (It happened to be covered by the pooled Polymarket position, but the alert did not
know that and had no business claiming it.)

**3. "Lifetime" says which lifetime it means.** Overnight on Aug 4 the alerts read `Lifetime: +$33.62`;
the settled truth after the morning was `+$32.38`. The difference is today's fill-time *estimate* of
pairs that had not settled. Now: `Lifetime: +$32.38 settled (+$33.62 including today's estimate)`, and
where the caller has only the compound number it is at least labelled `(settled + today's estimate)`.

**4. A naked remainder settling says what it is.** `[UNTRACKED naked]` settled rows now read
*"the UNHEDGED REMAINDER of this trade … a one-way bet, so this is luck, not edge. The hedged pair on
this game is reported separately."* — so a +$1.80 beside a −$2.27 reads as one trade, not two.

---

## T5 — The Kalshi maker fee, measured

`kalshi_maker_fee_series` sat empty for the life of this bot under a comment calling it a *"verified
list"*. Nothing had ever verified it. So we read our own fill history: **`GET /portfolio/fills`,
2026-07-22 → 2026-08-04, 91 fills, 46 of them MAKER (`is_taker: false`)**.

**Two separate facts came out.**

### What the fee is, where it is charged

```
maker fee = ceil( 0.0175 · C · p · (1 − p), 4dp )
```

Exactly a **quarter** of the 0.07 taker rate, over the same `p·(1−p)` base. All **8/8** fee-charging
maker fills reproduce to the cent:

| series | count | yes price | predicted | actual |
|---|---:|---:|---:|---:|
| KXMLBGAME | 17.00 | 0.06 | 0.016800 | 0.016800 |
| KXWTAMATCH | 54.00 | 0.37 | 0.220300 | 0.220300 |
| KXWTAMATCH | 25.00 | 0.79 | 0.072600 | 0.072600 |
| KXATPMATCH | 20.00 | 0.98 | 0.006900 | 0.006900 |
| KXMLBGAME | 2.05 | 0.98 | 0.000800 | 0.000800 |
| KXMLBGAME | 17.95 | 0.98 | 0.006200 | 0.006200 |
| KXMLBGAME | 20.00 | 0.99 | 0.003500 | 0.003500 |
| KXMLBGAME | 21.00 | 0.94 | 0.020800 | 0.020800 |

A flat per-contract guess — the obvious wrong model — misses the 54-share WTA fill by an order of
magnitude, because the fee has the `p·(1−p)` shape and is ~28× larger at 50¢ than at 2¢.

### Which series charge it

| charged | not charged (exactly $0.00) |
|---|---|
| KXMLBGAME, KXATPMATCH, KXWTAMATCH | KXCLUBFTOTAL, KXUECLTOTAL, KXUCLTOTAL, KXMLSTEAMTOTAL, KXDIMAYORTOTAL, KXARGPREMDIVTOTAL, KXLIGAMXTOTAL, KXEKSTRAKLASATOTAL, KXUFCFIGHT |

**38/38** maker fills on soccer and UFC series were charged exactly **$0.00** — including one of **353
shares** and one of **179 shares**, far too much size for a zero to be a rounding artefact.

### What changed

`quotes.rest_maker_fee(price, rate)` is subtracted from both the floor and `net_at_quote` for
rest-kalshi. The rate comes from `cfg.kalshi_maker_fee_rates` (`{KXMLBGAME, KXATPMATCH, KXWTAMATCH: 0.0175}`);
a series absent from the map prices at zero, so **soccer and UFC quoting is byte-identical** (there is a
test asserting exactly that). The floor takes one Newton step because the fee depends on the price being
solved for; the residual is < 1e-4, four orders of magnitude below the tick.

`kalshi_maker_fee_series` (the hard-refusal list) is **left empty and left alone** — a fee that is priced
does not need a series banned. Its stale comment is corrected to say why it is empty now.

### Effect on current p50 edges

Per-share cost: 0.0831% at 5¢/95¢, 0.3281% at 25¢/75¢, peaking at **0.4375%** at 50¢ — against a
`target_net` of 0.6%, so an MLB/tennis rest-kalshi quote at 50¢ now needs 1.0375% of gross edge where it
used to need 0.6%.

Share of rails-ok achievable samples that clear the 0.6% target, before → after:

| sport\|phase | n | ≥0.60% (before) | ≥0.93% (25¢ fee) | ≥1.04% (50¢ fee) |
|---|---:|---:|---:|---:|
| mlb\|pre | 5,000 | 0.00% | 0.00% | 0.00% |
| mlb\|inplay | 5,000 | 31.40% | 26.44% | **25.60%** |
| mlb\|gap | 3,568 | 0.00% | 0.00% | 0.00% |
| tennis\|pre | 5,000 | 2.40% | 2.40% | **2.36%** |
| tennis\|inplay | 5,000 | 0.00% | 0.00% | 0.00% |
| soccer\|pre | 5,000 | 14.92% | — | — (rate 0, unaffected) |
| ufc\|pre | 5,000 | 0.00% | — | — (rate 0, unaffected) |

Read: p50 achievable is already deeply negative on MLB pre (−1.80%) and tennis pre (−0.94%), so at the
median the fee changes nothing that was going to trade. It bites on **mlb\|inplay**, where the viable
share falls from 31.4% to ~25.6% — those are quotes we were placing at an edge that was overstated by up
to 0.44¢/share. Both MLB and tennis are still `MEASURING` on `--gates` (0 clean hedged fills each), so no
realised edge is affected yet; this correction lands *before* those samples are collected rather than
after.

---

## T6 — The excess-share tracer (report only, nothing changed)

### Why 6.5722 sh rode unhedged on 26AUG04JEJBMU O/U5.5

Not a partial hedge fill, not floored sizing, not a hedge-side minimum. It is the **2-tick marketable
limit on a spend-the-full-amount market BUY**.

`poly_exec.place_market_buy` sends a BUY at `best_ask + 2 ticks` (ceiled to a cent) with
`size = floor(target_shares)`. On Polymarket a market BUY spends the whole USDC maker amount
(`limit × size`); any price improvement between that limit and the actual resting asks comes back as
**extra shares, not a refund**. Predicted vs the venue, to six decimals:

| target sh | floor | limit | fill | predicted shares | venue shares | excess vs target |
|---:|---:|---:|---:|---:|---:|---:|
| 14.91 | 14 | 0.31 | 0.29 | 14.965517 | 14.965516 | +0.0555 |
| 44.74 | 44 | 0.31 | 0.29 | 47.034483 | 47.034481 | +2.2945 |
| 76.00 | 76 | 0.38 | 0.36 | 80.222222 | 80.222221 | +4.2222 |
| | | | | | **TOTAL** | **+6.5722** |

The overshoot is `2 ticks ÷ fill price`, so it is **worse the cheaper the hedge leg**: +2.1% at 95¢,
+5.6% at 36¢, +6.9% at 29¢, **+20% at 10¢, +40% at 5¢**.

### Cumulative naked-remainder P&L

All `[UNTRACKED naked]` settled rows total **+$39.13 on $128.61** — but that is dominated by the +$42 UFC
ghost-order stack and a +$0.40 ATP row, which were naked **orders**, not over-bought hedges. Lumping them
in makes an unprofitable habit look like a windfall. **The hedge-overshoot remainders alone:**

| date | market | sh | cost | outcome | ROI | sellable (≥5 sh)? |
|---|---|---:|---:|---:|---:|---|
| 2026-07-29 | 26JUL29TOTSYD total_goals\|4.5 | 18.3489 | 6.65 | −6.6456 | −99.9% | yes |
| 2026-07-31 | 26JUL31BCBAR total_goals\|4.5 | 1.5000 | 0.44 | −0.4399 | −100.0% | no — below the 5-share Poly minimum |
| 2026-08-01 | 26JUL31JUAPUM total_goals\|5.5 | 6.5143 | 0.48 | +6.0355 | +1257.4% | yes |
| 2026-08-04 | 26AUG04JEJBMU total_goals\|1.5 | 1.2298 | 1.17 | +0.0559 | +4.8% | no — below the minimum |
| 2026-08-04 | 26AUG04JEJBMU total_goals\|5.5 | 6.5722 | 2.27 | −2.2737 | −100.2% | yes |
| | **TOTAL** | | **11.01** | **−3.2678** | **−29.7%** | 3 of 5 sellable |

Five events, five overshoots — this happens on **every** rest-kalshi hedge, not occasionally. Realised
−$3.27 on $11.01 of stake, on n=5 with single events ranging −$6.65 to +$6.04. The expected value of a
naked share bought at the market price is ≈ 0 before costs, so the −29.7% is small-sample noise; what is
*not* noise is the variance, which is ±$6 swings on a strategy whose whole pitch is a locked 0.6–1%.

### Options — your call

**A. Accept and ride (status quo).** Zero implementation. EV ≈ 0 before costs. Cost: ±$6-per-event
variance sitting on top of a locked-edge strategy, and it grows with hedge size — at the current $70
quote cap a 10¢ hedge leg would ride ~20% of the position naked.

**B. Sweep-sell the excess above a threshold.** Only 3 of 5 events were sellable (Polymarket's minimum is
5 shares; below that it is genuinely un-closable dust, which is what `_book_dust` already handles).
Flatten cost is a taker sale — 1 tick of spread + `0.05 · min(p, 1−p)` fee:

| market | sh | @ | fee | spread | total | % of stake |
|---|---:|---:|---:|---:|---:|---:|
| 26JUL29TOTSYD | 18.3489 | 0.362 | $0.333 | $0.183 | $0.516 | 7.8% |
| 26JUL31JUAPUM | 6.5143 | 0.074 | $0.024 | $0.065 | $0.089 | 18.6% |
| 26AUG04JEJBMU | 6.5722 | 0.345 | $0.114 | $0.066 | $0.179 | 7.9% |
| | | | | | **$0.78** | |

So ~**$0.78 of certain cost** to remove the three ±$6 exposures (vs −$2.88 actually realised on them).
Note the fee is a *fraction of the stake*, so it is worst on cheap legs — exactly the legs where the
overshoot is largest.

**C. (Not asked for, but it is the root cause and the cheapest fix.)** Don't over-buy in the first place:
size the USDC maker amount from an expected fill price rather than from the padded limit, or place the
hedge as a marketable limit with share semantics. That takes the excess to ≈0 for **$0** of fees and
covers the sub-5-share cases that B structurally cannot. The reason the 2-tick padding exists is to
survive a book that moves between the read and the send, so this trades a *known* over-buy for a
*possible* under-hedge — which is why it is a decision and not a patch. Recommendation: **C, with B as
the fallback for whatever remainder survives.**

---

## Open items (not actioned, flagged for a decision)

1. **−$0.0994 of naked share dust** from the three unwinds (mostly DIMCAL's 0.72 sh that Kalshi would not
   sell) is confirmed against venue cash but deliberately **not** booked — it is not exit cost, and
   sweeping it into the exits bucket would misname it. It is the residual in the table above.
2. **`auto_flattened` never reaches lifetime.** `state.record()` only inspects
   `hedge_declined | hedge_unwound | unwind_FAILED`, so an `auto_flattened` row touches neither
   `unwind_cost_today` nor the new exits bucket. It has **never fired** (0 rows across every CSV), so
   nothing is currently mis-stated — but it is a verified exit that pays real cash, and it is the same
   class of hole this document exists to close. Left alone because the task specified exactly which
   three events may book, and widening that quietly is how a bookkeeping change becomes a surprise.

---

## Verification

- **`pytest`: 1374 passed** (was 1329 + 5 outdated; 45 new). No test writes live runtime state — the
  existing guard is intact and `test_every_runtime_file_is_registered` was updated for the new
  `restatements` entry.
- **`python -m src.genz.maker_rt --gates`** — runs clean; unchanged output (soccer 25 fills EDGE-POSITIVE,
  mlb/tennis/ufc still MEASURING).
- **`python -m src.genz.maker_rt --balance`** — runs clean (and the cp1252 crash above is fixed).

### The books line the NEXT 8h balance check should print

```
   My books: settled lifetime +$25.39 (+$24.01 from hedged pairs, +$8.38 luck, -$6.99 exit costs) · today's locked estimate +$X.XX (not settled yet — it is still in the positions above)
```

`+$25.39 / +$24.01 / +$8.38 / -$6.99` are exact, assuming nothing further settles before the slot; the
locked estimate is whatever the day holds. That check should also print the one-time
`📌 NEW BASELINE (baseline_v2)` line, since the 2026-07-31 baseline was snapped with the Birmingham pair
open and the next flat snapshot re-anchors it.
