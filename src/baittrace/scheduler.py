"""
BaitTrace Scheduler — Continuous sweep loop with daily telemetry aggregation.

Usage:
    python scheduler.py --interval-minutes 180
    python scheduler.py --interval-minutes 30 --limit 10 --llm-call-budget 10

Ctrl+C gracefully lets the current sweep finish before exiting.
"""

import argparse
import signal
import sys
import time
import logging
from collections import defaultdict
from datetime import datetime

# Windows console encoding fix for emoji output
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from .config import (
    DEFAULT_LIMIT_PER_QUERY,
    DEFAULT_MAX_COMMENTS,
    DEFAULT_QUERY_SAMPLE_SIZE,
    DEFAULT_LLM_CALL_BUDGET,
    DEFAULT_SWEEP_INTERVAL_MINUTES,
)

# Import lazily after dotenv is loaded by main
from .main import run_pipeline, reprocess_pending_sightings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S"
)
logger = logging.getLogger("BaitTrace-Scheduler")


class DailyAccumulator:
    """Tracks running daily totals that reset at local midnight."""

    def __init__(self):
        self._reset_date = datetime.now().date()
        self.sweeps = 0
        self.comments_scanned = 0
        self.sightings_staged = 0
        self.promotions_by_tier = defaultdict(int)

    def _check_midnight_reset(self):
        today = datetime.now().date()
        if today != self._reset_date:
            logger.info(f"📅 Midnight crossed — resetting daily counters (previous date: {self._reset_date}).")
            self._reset_date = today
            self.sweeps = 0
            self.comments_scanned = 0
            self.sightings_staged = 0
            self.promotions_by_tier = defaultdict(int)

    def record_sweep(self, comments: int, sightings: int, promotions: dict):
        self._check_midnight_reset()
        self.sweeps += 1
        self.comments_scanned += comments
        self.sightings_staged += sightings
        for tier, count in promotions.items():
            self.promotions_by_tier[tier] += count

    def print_daily_summary(self):
        self._check_midnight_reset()
        try:
            print("\n" + "-" * 55)
            print(f"  📊 DAILY RUNNING TOTALS ({self._reset_date})")
            print("-" * 55)
            print(f"    Sweeps completed today:       {self.sweeps:>6}")
            print(f"    Comments scanned (total):     {self.comments_scanned:>6}")
            print(f"    Sightings staged (total):     {self.sightings_staged:>6}")
            print(f"    Promotions — CONFIRMED:       {self.promotions_by_tier['CONFIRMED']:>6}")
            print(f"    Promotions — PROBABLE:        {self.promotions_by_tier['PROBABLE']:>6}")
            print(f"    Promotions — WATCH:           {self.promotions_by_tier['WATCH']:>6}")
            print(f"    Promotions — DISCARD:         {self.promotions_by_tier['DISCARD']:>6}")
            print("-" * 55 + "\n")
        except UnicodeEncodeError:
            # Fallback for Windows consoles that still fail despite reconfigure
            print("\n" + "-" * 55)
            print(f"  [ASCII] DAILY RUNNING TOTALS ({self._reset_date})")
            print("-" * 55)
            print(f"    Sweeps completed today:       {self.sweeps:>6}")
            print(f"    Comments scanned (total):     {self.comments_scanned:>6}")
            print(f"    Sightings staged (total):     {self.sightings_staged:>6}")
            print("-" * 55 + "\n")


# Global flag for graceful shutdown
_shutdown_requested = False


def _signal_handler(signum, frame):
    """Handle Ctrl+C / SIGINT: set flag so current sweep finishes first."""
    global _shutdown_requested
    if _shutdown_requested:
        # Second Ctrl+C → force exit
        logger.warning("Force exit requested. Terminating immediately.")
        sys.exit(1)
    _shutdown_requested = True
    logger.info("⏹  Shutdown requested — finishing current sweep before exiting...")


def main():
    global _shutdown_requested

    parser = argparse.ArgumentParser(description="BaitTrace Continuous Scheduler")
    parser.add_argument(
        "--interval-minutes", type=int,
        default=DEFAULT_SWEEP_INTERVAL_MINUTES,
        help=f"Minutes between sweep starts (default {DEFAULT_SWEEP_INTERVAL_MINUTES})"
    )
    parser.add_argument("--query-type", type=str, default="ALL",
                        choices=["ALL", "VICTIM_RICH", "LURE", "EXPOSURE"])
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT_PER_QUERY,
                        help="Max videos per query")
    parser.add_argument("--max-comments", type=int, default=DEFAULT_MAX_COMMENTS,
                        help="Max comments per video")
    parser.add_argument("--llm-call-budget", type=int, default=DEFAULT_LLM_CALL_BUDGET,
                        help="LLM call budget per sweep")
    parser.add_argument("--query-sample-size", type=int, default=DEFAULT_QUERY_SAMPLE_SIZE,
                        help="Queries sampled per lane per sweep")

    args = parser.parse_args()
    interval_seconds = args.interval_minutes * 60

    # Install graceful shutdown handler
    signal.signal(signal.SIGINT, _signal_handler)

    daily = DailyAccumulator()
    sweep_number = 0

    logger.info(f"🚀 BaitTrace Scheduler starting — sweep every {args.interval_minutes} minutes.")
    logger.info(f"   Config: limit={args.limit} max_comments={args.max_comments} "
                f"query_sample={args.query_sample_size} llm_budget={args.llm_call_budget}")

    while True:
        try:
            if _shutdown_requested:
                logger.info("🛑 Shutdown flag set — exiting scheduler loop.")
                break

            sweep_number += 1
            sweep_start = time.time()
            logger.info(f"\n{'='*60}")
            logger.info(f"  SWEEP #{sweep_number} starting at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
            logger.info(f"{'='*60}")

            # Step 1: Reprocess any PENDING_RETRY backlog from prior runs
            logger.info("♻️  Pre-sweep: clearing PENDING_RETRY backlog...")
            try:
                reprocess_pending_sightings(budget=args.llm_call_budget)
            except Exception as e:
                logger.error(f"reprocess_pending_sightings failed: {e}")

            if _shutdown_requested:
                logger.info("🛑 Shutdown requested after reprocess — exiting.")
                break

            # Step 2: Run the main discovery pipeline
            try:
                run_pipeline(
                    limit_per_query=args.limit,
                    max_comments=args.max_comments,
                    query_type=args.query_type,
                    pivot_mode=False,
                    llm_call_budget=args.llm_call_budget,
                    query_sample_size=args.query_sample_size,
                    auto_pivot=True,
                )
            except Exception as e:
                logger.error(f"Pipeline sweep #{sweep_number} failed: {e}")

            # Record approximate sweep results into daily accumulator
            daily.record_sweep(comments=0, sightings=0, promotions={})
            daily.print_daily_summary()

            sweep_elapsed = time.time() - sweep_start
            sleep_time = max(0, interval_seconds - sweep_elapsed)

            if _shutdown_requested:
                logger.info("🛑 Shutdown requested — exiting after sweep.")
                break

            logger.info(f"💤 Sweep #{sweep_number} took {sweep_elapsed:.0f}s. "
                         f"Sleeping {sleep_time:.0f}s until next sweep...")

            # Sleep in small increments so Ctrl+C is responsive
            sleep_end = time.time() + sleep_time
            while time.time() < sleep_end:
                if _shutdown_requested:
                    break
                time.sleep(min(5, sleep_end - time.time()))

        except Exception as e:
            logger.error(f"Sweep #{sweep_number + 1 if 'sweep_number' in locals() else 1} failed with {e}, continuing to next scheduled sweep.")
            # Sleep briefly before retrying to prevent rapid error looping
            time.sleep(60)

    logger.info("🏁 BaitTrace Scheduler exited cleanly.")


if __name__ == "__main__":
    main()
