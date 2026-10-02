"""
BaitTrace — Centralized Configuration Constants

All tuneable defaults live here. CLI flags in main.py / scheduler.py still
override at runtime, but these provide a single source of truth for default
values so they stop drifting between modules and rounds.
"""
import os

# Discovery
DEFAULT_LIMIT_PER_QUERY: int = 20
DEFAULT_MAX_COMMENTS: int = 50
DEFAULT_QUERY_SAMPLE_SIZE: int = 10

# LLM
DEFAULT_LLM_CALL_BUDGET: int = 15

# --- Classifier Provider ---
# "jev"    — TypeSafe Jev via OpenRouter Decisions API (default, cheap & fast)
# "gemini" — Original all-Gemini path (fallback / comparison)
CLASSIFIER_PROVIDER: str = os.environ.get("CLASSIFIER_PROVIDER", "jev")

# QUERY_GEN_PROVIDER is NOT configurable — always "gemini".
# Jev is a structured-decision model that cannot generate free-form text,
# so query generation (generate_dynamic_queries / any future LLM-generated
# queries) must always use Gemini.  Do NOT "fix" this to support Jev.
QUERY_GEN_PROVIDER: str = "gemini"

# --- Jev Budget ---
# Output tokens are free and input is cheap, so allow a generous call count.
# The dollar ceiling is the hard stop: even if call count is under budget,
# stop early once cumulative usage.cost this sweep exceeds the ceiling.
JEV_LLM_CALL_BUDGET: int = int(os.environ.get("JEV_LLM_CALL_BUDGET", "300"))
JEV_COST_CEILING_PER_SWEEP: float = float(os.environ.get("JEV_COST_CEILING_PER_SWEEP", "0.50"))

# Auto-Pivot
PIVOT_THRESHOLD: int = 20
PIVOT_MAX_HANDLES_PER_RUN: int = 5

# Scheduler
DEFAULT_SWEEP_INTERVAL_MINUTES: int = 180
