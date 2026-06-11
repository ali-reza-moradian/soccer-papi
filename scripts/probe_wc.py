"""One-off probe: find the live FIFA World Cup tournament ID(s) and confirm Pinnacle returns fixtures.

Usage:
    ODDS_PAPI_KEY=... python -m scripts.probe_wc

Steps (exactly what Bug 2 asks for):
  1. GET /v4/tournaments?sportId=10 and print every tournament whose name/slug matches
     /world.cup/i or /fifa/i — full object incl. counts.
  2. Rank candidates by futureFixtures/upcomingFixtures/liveFixtures (non-zero = active now).
  3. GET /v4/odds-by-tournaments?bookmaker=pinnacle for each candidate and report how many
     fixtures actually come back, so we only pin IDs that truly return odds.
"""
from __future__ import annotations

import json
import os
import re
import sys

from src.oddspapi import OddsPapiClient
from src.run import _fixture_list

SPORT_ID = 10
PAT = re.compile(r"world.?cup|fifa", re.IGNORECASE)


def main() -> int:
    key = os.environ.get("ODDS_PAPI_KEY")
    if not key:
        print("ERROR: set ODDS_PAPI_KEY in the environment first.", file=sys.stderr)
        return 1
    client = OddsPapiClient(key)

    print("=== /v4/tournaments?sportId=10 — matches /world.cup/i or /fifa/i ===")
    tours = client.tournaments(sport_id=SPORT_ID)
    if not isinstance(tours, list):
        print("Unexpected /v4/tournaments payload:", type(tours))
        return 1

    matched = [t for t in tours
               if PAT.search(t.get("tournamentName") or "") or PAT.search(t.get("tournamentSlug") or "")]
    matched.sort(key=lambda t: -((t.get("liveFixtures") or 0) + (t.get("upcomingFixtures") or 0)
                                 + (t.get("futureFixtures") or 0)))
    for t in matched:
        print(json.dumps(t, ensure_ascii=False))

    # Candidates: anything with at least one live/upcoming/future fixture right now.
    candidates = [t for t in matched
                  if (t.get("futureFixtures") or 0) or (t.get("upcomingFixtures") or 0)
                  or (t.get("liveFixtures") or 0)]
    print("\n=== candidates with > 0 fixtures (live/upcoming/future) ===")
    for t in candidates:
        print(f"  id={t.get('tournamentId')} | {t.get('tournamentName')} "
              f"| future={t.get('futureFixtures')} upcoming={t.get('upcomingFixtures')} "
              f"live={t.get('liveFixtures')}")

    print("\n=== /v4/odds-by-tournaments?bookmaker=pinnacle — confirm fixtures return ===")
    confirmed: list[int] = []
    for t in candidates:
        tid = int(t["tournamentId"])
        try:
            payload = client.odds_by_tournaments([tid], bookmaker="pinnacle")
        except Exception as exc:  # noqa: BLE001 - probe: report and continue
            print(f"  id={tid:<7} ERROR: {exc}")
            continue
        fixtures = _fixture_list(payload)
        n_with_odds = sum(1 for fx in fixtures if fx.get("bookmakerOdds"))
        print(f"  id={tid:<7} pinnacle -> {len(fixtures)} fixture(s), {n_with_odds} with odds "
              f"| {t.get('tournamentName')}")
        if fixtures:
            confirmed.append(tid)

    print("\n=== RESULT ===")
    print("Pin these tournamentId(s) (Pinnacle returned fixtures):", confirmed or "NONE")
    return 0


if __name__ == "__main__":
    sys.exit(main())
