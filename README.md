# Anti-YTScamify (BaitTrace)

Autonomous OSINT & Threat Intelligence engine designed to detect, track, and dismantle coordinated scam operations and financial fraud funnels on YouTube.

## 🛡️ Architecture & Pipeline

BaitTrace operates via an end-to-end multi-stage pipeline:

1. **Discovery (`discovery.py`)**: Uses an LLM threat matrix to generate evasion-resistant search vectors across crypto, task scam, and investment fraud niches.
2. **Extraction (`extractor.py`)**: Scrapes and normalizes high-risk comments and metadata using `yt-dlp` and `youtube-comment-downloader`.
3. **Parser (`parser.py`)**: Precision regex and unicode normalizer for extracting evasive Telegram handles, WhatsApp numbers, obfuscated domains, and contact triggers.
4. **Heuristics (`heuristics.py`)**: Fast rule-based filtering for threat scoring and bot-farm behavioral signals.
5. **AI Brain (`brain.py`)**: Deep reasoning model evaluating scam intent, vector categorization, and confidence scoring.
6. **Enricher & Sync (`enricher.py`, `main.py`)**: Supabase sync for seen videos, deduplication, and lead persistence.
7. **Command & Control (`server.py`, `dashboard.html`)**: Real-time FastAPI WebSocket dashboard for live telemetry and threat monitoring.

## 🚀 Getting Started

### Prerequisites
- Python 3.10+
- Supabase Project & API Keys
- Gemini API Key

### Installation

```bash
# Clone the repository
git clone https://github.com/VenkateshReddy007/Anti-YTScamify.git
cd Anti-YTScamify

# Create and activate a virtual environment
python -m venv venv
# On Windows (cmd):
venv\Scripts\activate
# On Linux/macOS:
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt
```

### Configuration

Copy `.env.example` to `.env` and fill in your credentials:
```bash
cp .env.example .env
```

```ini
SUPABASE_URL=https://your-project.supabase.co
SUPABASE_KEY=your-supabase-service-role-key
GEMINI_API_KEY=your-gemini-api-key
```

> **Security Note:** Never commit your `.env` file to version control.

### Running the Pipeline

```bash
# Single execution run (20 videos per query, up to 50 comments each, all query funnels)
python main.py --once --limit 20 --max-comments 50 --query-type ALL

# Start the Live Monitoring Dashboard server
python server.py
```
Open `http://localhost:8000` to access the live dashboard.

### Running BaitTrace Continuously

Use `scheduler.py` to run sweeps in a loop with automatic PENDING_RETRY
clearing and daily telemetry accumulation:

```bash
# Default: sweep every 3 hours
python scheduler.py

# Custom interval (e.g. every 30 minutes for testing)
python scheduler.py --interval-minutes 30

# Full configuration
python scheduler.py --interval-minutes 180 --limit 20 --max-comments 50 \
    --llm-call-budget 15 --query-sample-size 10 --query-type ALL
```

**How it works:**

1. Before each discovery sweep, the scheduler calls `reprocess_pending_sightings()`
   to clear any PENDING_RETRY backlog from a prior quota-exhausted run.
2. Runs `run_pipeline()` with auto-pivot enabled (pass `--no-auto-pivot` to main.py
   to disable).
3. At the end of each sweep, prints a running daily total (comments scanned,
   sightings staged, promotions by tier) that resets at local midnight.
4. **Ctrl+C** is handled gracefully: the current sweep's flush calls finish before
   the process exits. Press Ctrl+C a second time to force-quit immediately.

**Expected log cadence:** Each sweep takes 5–20 minutes depending on `--limit` and
video comment density. Between sweeps, the scheduler sleeps for `--interval-minutes`.

### Running Tests

```bash
python -m pytest tests/ -v
```

## 📄 License
MIT License