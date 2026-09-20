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
# Single execution run (5 videos, up to 35 comments each, all query funnels)
python main.py --once --limit 5 --max-comments 35 --query-type ALL

# Start the Live Monitoring Dashboard server
python server.py
```
Open `http://localhost:8000` to access the live dashboard.

### Running Tests

```bash
python -m pytest tests/ -v
```

## 📄 License
MIT License