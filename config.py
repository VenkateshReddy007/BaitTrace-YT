"""
BaitTrace — Centralized Configuration Constants

All tuneable defaults live here. CLI flags in main.py / scheduler.py still
override at runtime, but these provide a single source of truth for default
values so they stop drifting between modules and rounds.
"""

# Discovery
DEFAULT_LIMIT_PER_QUERY: int = 20
DEFAULT_MAX_COMMENTS: int = 50
DEFAULT_QUERY_SAMPLE_SIZE: int = 10

# LLM
DEFAULT_LLM_CALL_BUDGET: int = 15

# Auto-Pivot
PIVOT_THRESHOLD: int = 20
PIVOT_MAX_HANDLES_PER_RUN: int = 5

# Scheduler
DEFAULT_SWEEP_INTERVAL_MINUTES: int = 180
