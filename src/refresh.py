"""Refresh the cached catalogs (sports/bookmakers/markets/tournaments).

Run by the refresh-catalog workflow (weekly + manual). Spends ~4 billable requests,
then prints the resolved friendlies tournament IDs so they can be pinned in config.yaml.

    python -m src.refresh
"""
from __future__ import annotations

import sys

from . import catalog
from .config import load_config
from .logsetup import setup_logging
from .oddspapi import OddsPapiClient, QuotaExceeded, check_budget, log_key_exhausted


def main() -> int:
    log = setup_logging()
    cfg = load_config()

    if not cfg.secrets.odds_papi_key:
        log.error("ODDS_PAPI_KEY not set — cannot refresh catalogs.")
        return 0

    client = OddsPapiClient(cfg.secrets.odds_papi_key, logger=log)

    safety = int(cfg.budget_opt("safety_margin", 15))
    try:
        acct = check_budget(client, safety, log)
    except QuotaExceeded as exc:
        log_key_exhausted(log, exc)
        return 0
    # A refresh costs ~4 requests; make sure we have comfortable headroom.
    remaining = acct.get("remaining")
    if remaining is not None and remaining <= safety + 4:
        log.warning("Only %s requests remain (margin %s + 4 needed). Skipping refresh.", remaining, safety)
        return 0

    try:
        counts = catalog.refresh_catalogs(client, cfg.cache_dir, cfg.sport_id, log)
    except QuotaExceeded as exc:
        log.warning("Quota hit during refresh. Partial catalogs may be written.")
        log_key_exhausted(log, exc)
        return 0

    # Resolve and print friendlies IDs so the user can pin them.
    tours = catalog.load_json(cfg.cache_dir, catalog.TOURNAMENTS_FILE) or []
    matched = catalog.resolve_tournament_ids(tours, cfg.tournament_regex, cfg.national_teams_only)
    if matched:
        log.info("Friendlies tournaments matched (pin these in config.yaml tournaments.pinned_ids):")
        for t in matched:
            log.info("  id=%s  name=%r  category=%r  upcoming=%s  future=%s",
                     t.get("tournamentId"), t.get("tournamentName"), t.get("categoryName"),
                     t.get("upcomingFixtures"), t.get("futureFixtures"))
        log.info("  pinned_ids: %s", [t.get("tournamentId") for t in matched])
    else:
        log.warning("No friendlies tournaments matched regex %r.", cfg.tournament_regex)

    log.info("Refresh complete. Catalog sizes: %s. Billable requests used: %s",
             counts, client.billable_count)
    return 0


if __name__ == "__main__":
    sys.exit(main())
