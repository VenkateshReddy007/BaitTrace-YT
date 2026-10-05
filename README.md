<div align="center">

<img src="https://capsule-render.vercel.app/api?type=waving&color=gradient&customColorList=0,2,2,5,30&height=220&section=header&text=BAITTRACE&fontSize=70&fontColor=ffffff&animation=fadeIn&fontAlignY=38&desc=Autonomous%20Threat-Intelligence%20Sensor%20for%20YouTube%20Comment%20Scam%20Networks&descAlignY=58&descSize=18" width="100%"/>

<img src="https://readme-typing-svg.demolab.com/?lines=Scammers+don't+advertise.;They+hide+in+the+comments+of+videos+they+never+made.;BaitTrace+finds+them+anyway.&font=Fira+Code&center=true&width=700&height=50&color=F77F00&vCenter=true&size=22&pause=1200"/>

<br>

<img src="https://img.shields.io/badge/PYTHON-3.10+-3776AB?style=for-the-badge&logo=python&logoColor=white&labelColor=000000"/>
<img src="https://img.shields.io/badge/FASTAPI-LIVE%20DASHBOARD-009688?style=for-the-badge&logo=fastapi&logoColor=white&labelColor=000000"/>
<img src="https://img.shields.io/badge/SUPABASE-POSTGRES-3ECF8E?style=for-the-badge&logo=supabase&logoColor=white&labelColor=000000"/>
<img src="https://img.shields.io/badge/JEV-SYSTEM%20ONE%20MODEL-FF4800?style=for-the-badge&labelColor=000000"/>
<img src="https://img.shields.io/badge/GEMINI-3.8%20FLASH-4285F4?style=for-the-badge&logo=googlegemini&logoColor=white&labelColor=000000"/>

<img src="https://raw.githubusercontent.com/andreasbm/readme/master/assets/lines/rainbow.gif" width="100%">

</div>

<br>

## THE PROBLEM

Every day, thousands of Indian YouTube videos — beginner stock-market tutorials, government-job walkthroughs, loan explainers, "how to earn as a student" content — sit underneath comment sections quietly infested with recruiter bots. A fake trading mentor drops a WhatsApp number. A sock-puppet account vouches for a Telegram channel promising ₹500/day for rating tasks. A colour-prediction betting bot links a "proof" screenshot.

Nobody moderates this surface. The videos themselves are completely legitimate. The scam lives entirely in the replies.

**BaitTrace hunts there, continuously, autonomously.**

<br>

## WHAT MAKES THIS DIFFERENT

Most scam-detection tooling stops at *"does this one comment look suspicious?"* — a single LLM call, a yes/no answer, done. That approach misses the single strongest signal available: **the same handle, posted by different throwaway accounts, across completely unrelated videos, using near-identical phrasing.** One comment is a coin flip. The same handle on eight videos from eight burner accounts is not.

BaitTrace is built around that insight from the ground up. It does not score comments. It builds **campaigns**.

<br>

<img src="https://raw.githubusercontent.com/andreasbm/readme/master/assets/lines/rainbow.gif" width="100%">

<br>

## THE AI ARCHITECTURE — TWO MODELS, TWO DIFFERENT JOBS

Most pipelines bolt one LLM onto everything. BaitTrace deliberately splits cognition across two fundamentally different kinds of models, each doing only what it is actually good at.

<br>

<table width="100%">
<tr>
<td width="50%" valign="top">

### JEV — THE CLASSIFIER
**via OpenRouter**

[Jev](https://openrouter.ai/docs/guides/community/jev.md) is TypeSafe AI's **System One Model** — not a chat model, a *typed decision engine*. It does not generate text. It answers calibrated, structured questions directly, and returns a probability, not a performance.

Every classification BaitTrace makes — fraud detection, role attribution, campaign-tier judgment — runs through Jev. Because it is structurally incapable of generating free text, it is structurally incapable of hallucinating an explanation, a quote, or a justification that was never there.

That is not a limitation worked around. That is the entire reason it can be trusted for this job.

</td>
<td width="50%" valign="top">

### GEMINI — KEPT DELIBERATELY NARROW
**3.8 Flash**

Gemini is not the workhorse here, on purpose. It is reserved for exactly two things:

**Dynamic query generation** — once per sweep, proposing fresh search angles from recently confirmed scam patterns, because inventing new search phrases is pure text generation, the one thing Jev cannot do.

**Explaining promoted leads** — once a handle crosses into `PROBABLE` or `CONFIRMED`, and only then, Gemini writes one human-readable sentence for the dashboard. A tiny fraction of total volume, by design.

</td>
</tr>
</table>

<br>

| Primitive | Used for | Returns |
|---|---|---|
| `noul` | Is this comment actively recruiting a victim? | A calibrated probability — not a guess dressed up as confidence |
| `choice` | Is the commenter a RECRUITER, a VICTIM_REPORT, or NEUTRAL? | The pick, plus a native trained confidence score |
| `choice` | Scam-type classification | TASK_SCAM / RATING_JOB / CRYPTO_BETTING — typed, bounded, never invented |

> Most of this project's early pain came from treating a general-purpose LLM's rate limit as if it had to absorb every classification in the pipeline. Splitting a typed-decision workload onto a typed-decision model, and reserving the generative workload for a generative model, is what actually made this sustainable at scale.

<br>

<img src="https://raw.githubusercontent.com/andreasbm/readme/master/assets/lines/rainbow.gif" width="100%">

<br>

## ARCHITECTURE

```mermaid
flowchart TB
    subgraph Discovery["DISCOVERY LAYER"]
        direction LR
        A[Three Query Lanes] --> A1["VICTIM_RICH\nlegit high-traffic content"]
        A --> A2["LURE\nactive scam-bait content"]
        A --> A3["EXPOSURE\nvictim and awareness content"]
        G["Gemini\ngenerate_llm_queries()"] -.fresh angles from\nrecent confirmed patterns.-> A
        A1 & A2 & A3 --> YT[("yt-dlp search\n50+ multilingual queries\n6 Indian languages")]
    end

    subgraph Extraction["EXTRACTION LAYER"]
        direction LR
        YT --> CF[Comment Fetch — sort: recent]
        CF --> NORM["normalize_text()\nNFKD · zero-width strip\nleetspeak · spaced-digit collapse"]
        NORM --> REGEX["extract_indicators()\nTG_LINK · TG_MENTION · WA · PHONE · UPI_VPA\nstopword + PSP-allowlist guarded"]
        REGEX --> GATE["should_escalate()\nheuristic pre-classifier gate"]
    end

    subgraph Classify["CLASSIFICATION LAYER — JEV"]
        direction LR
        GATE --> JEV["Jev — typesafe/jev-1.13\nnoul: is_fraud\nchoice: role\nchoice: scam_type"]
        JEV --> ROLE{role}
        ROLE -->|RECRUITER| EVID[Evidence: scammer-authored]
        ROLE -->|VICTIM_REPORT| PROT[Protected — never attributed\nto the commenter]
        ROLE -->|NEUTRAL| SKIP[Discarded]
    end

    subgraph Campaign["CAMPAIGN SCORING LAYER"]
        direction LR
        EVID --> HIST[("Cross-run historical merge\ncandidate_sightings")]
        HIST --> SCORE["compute_campaign_score()\nvideo spread · author spread\nburner-account score\nsimhash duplicate clustering\ntemporal burst · indicator strength"]
        SCORE --> TIER{Tier}
        TIER -->|score 75+ or multi-video and multi-author| CONF[CONFIRMED]
        TIER -->|50-74| PROB[PROBABLE]
        TIER -->|25-49| WATCH[WATCH]
        TIER -->|below 25| DISC[DISCARD]
    end

    subgraph Loop["FEEDBACK LOOP"]
        direction LR
        CONF & PROB --> EXPLAIN["Gemini\nexplain_promoted_lead()"]
        CONF & PROB --> PIVOT["Auto-Pivot\nre-search + deep-scan known handles"]
        PIVOT --> YT
        EVID -->|RECRUITER hit| DEEP["Reply Thread Deep-Dive\nup to 50 replies per thread"]
        DEEP --> GATE
    end

    EXPLAIN --> DASH[("Live Dashboard\nFastAPI + WebSocket")]
    TIER --> DASH

    style JEV fill:#FF4800,stroke:#000,stroke-width:2px,color:#fff
    style G fill:#4285F4,stroke:#000,stroke-width:2px,color:#fff
    style EXPLAIN fill:#4285F4,stroke:#000,stroke-width:2px,color:#fff
    style CONF fill:#D7263D,stroke:#000,stroke-width:2px,color:#fff
    style PROB fill:#F77F00,stroke:#000,stroke-width:2px,color:#fff
    style WATCH fill:#1D7874,stroke:#000,stroke-width:2px,color:#fff
    style DISC fill:#555,stroke:#000,stroke-width:2px,color:#fff
```

<br>

<img src="https://raw.githubusercontent.com/andreasbm/readme/master/assets/lines/rainbow.gif" width="100%">

<br>

## PRECISION-FIRST DESIGN — WHY THIS DOES NOT CRY WOLF

A detector nobody trusts is worse than no detector. Every one of these exists because an earlier version got it wrong, caught it, and fixed it for good.

**Victim-aware classification.** On an exposure video, the person posting a scammer's handle is usually the victim, warning others — not the scammer. The `RECRUITER` / `VICTIM_REPORT` / `NEUTRAL` split means a victim's warning is never mistaken for a confession.

**Videos are never accused — handles are.** The evidence ledger shows "Sighted On," never "Scam Video." A legitimate stock-market tutorial with a recruiter comment underneath stays a legitimate video.

**Fail-closed, always.** If the classifier is unreachable, nothing gets manufactured as fraud. Every sighting carries an explicit `llm_status` — `EVALUATED`, `PENDING_RETRY`, or `FAILED_PARSE`. An outage produces silence, never a false accusation, and pending evidence is automatically re-queued the moment the classifier is reachable again.

**Regex with teeth, not vibes.** Telegram-handle extraction uses word-boundary lookarounds and a stopword rejection list — "telephone," "television," and "MTG option" cannot match. UPI extraction requires a real PSP-suffix allowlist — `support@mycompany` cannot match.

**No single comment can confirm a campaign.** `compute_campaign_score()` hard-blocks `CONFIRMED` tier unless there are at least two distinct videos and two distinct authors. One comment, however alarming, is capped at `PROBABLE` — evidence, not a verdict.

**Schema-contract tests.** A dedicated regression suite diffs every field the pipeline writes against the live database schema, specifically so a silently failing write — the single most expensive bug class this project hit, twice — can never happen again unnoticed.

<br>

<img src="https://raw.githubusercontent.com/andreasbm/readme/master/assets/lines/rainbow.gif" width="100%">

<br>

## FEATURE BREAKDOWN

<table width="100%">
<tr><th align="left" width="30%">System</th><th align="left">What it does</th></tr>
<tr><td><strong>Multilingual Discovery</strong></td><td>50+ queries across three intent lanes, in English, Hindi, Bengali, Malayalam, Telugu, Tamil, and Kannada</td></tr>
<tr><td><strong>LLM-Generated Query Expansion</strong></td><td>Gemini proposes new search angles each sweep based on recently confirmed scam patterns — the query set gets smarter as the sensor runs</td></tr>
<tr><td><strong>De-obfuscation Engine</strong></td><td>Defeats zero-width spaces, leetspeak, spaced-out digits, and NFKD-level unicode tricks before extraction ever runs</td></tr>
<tr><td><strong>Jev-Powered Triage</strong></td><td>Calibrated, structured, hallucination-resistant classification at the comment level</td></tr>
<tr><td><strong>Campaign Graph Scoring</strong></td><td>Weighted evidence model: video spread, author spread, burner-account heuristics, simhash near-duplicate clustering, temporal burst detection</td></tr>
<tr><td><strong>Cross-Run Historical Merge</strong></td><td>A handle seen once today and again next week is combined into one evolving campaign score — evidence compounds instead of resetting</td></tr>
<tr><td><strong>Auto-Pivot</strong></td><td>Once a handle clears a threat threshold, BaitTrace automatically re-searches YouTube for it and deep-scans every video where it reappears</td></tr>
<tr><td><strong>Reply Thread Deep-Dive</strong></td><td>A confirmed recruiter comment triggers a full reply-thread fetch — sock-puppet vouching replies are exactly what gets missed otherwise</td></tr>
<tr><td><strong>Author Cross-Referencing</strong></td><td>Burner-account authors get pivoted on by display name and channel, cross-checked against their own upload history</td></tr>
<tr><td><strong>Continuous Scheduler</strong></td><td>Dashboard-controlled sweep interval, graceful shutdown, automatic backlog clearing before every sweep</td></tr>
<tr><td><strong>Live Observability Dashboard</strong></td><td>Real-time WebSocket log stream, campaign threat ledger, and a staged-evidence view into everything still under evaluation</td></tr>
</table>

<br>

## DATA MODEL

Three tables, deliberately separated by what they represent.

| Table | Represents |
|---|---|
| `candidate_sightings` | Every indicator that survived extraction — the full, honest evidence log, fraud or not |
| `handles` | One row per unique handle ever sighted — the watchlist, always upserted regardless of tier |
| `actionable_leads` | Only `PROBABLE` / `CONFIRMED` — the output actually worth a human's attention |

<br>

<img src="https://raw.githubusercontent.com/andreasbm/readme/master/assets/lines/rainbow.gif" width="100%">

<br>

## TECH STACK

<div align="center">
<img src="https://skillicons.dev/icons?i=python,fastapi,postgres,supabase,githubactions&theme=dark"/>
</div>

<br>

| Layer | Technology |
|---|---|
| Ingestion | `yt-dlp`, `youtube-comment-downloader` |
| Classification | Jev (`typesafe/jev-1.13` via OpenRouter) |
| Query Generation & Lead Explanation | Gemini 3.8 Flash |
| Database | Supabase (Postgres) |
| Backend / Dashboard | FastAPI + WebSocket |
| Language | Python 3.10+ |

<br>

## GETTING STARTED

```bash
git clone <repo-url>
cd baittrace
pip install -e .

cp .env.example .env
# fill in SUPABASE_URL, SUPABASE_KEY, GEMINI_API_KEY, OPENROUTER_API_KEY

# apply scripts/schema.sql to your Supabase project, then:
python -m baittrace.main --once --limit 5 --max-comments 35

# or run the live dashboard:
python -m baittrace.server
```

**Testing**

```bash
python -m pytest tests/ -v
```

<br>

<div align="center">

<img src="https://raw.githubusercontent.com/andreasbm/readme/master/assets/lines/rainbow.gif" width="100%">

**BAITTRACE DOES NOT WAIT FOR A VICTIM TO COMPLAIN.**
**IT FINDS THE LURE BEFORE SOMEONE BITES.**

<img src="https://capsule-render.vercel.app/api?type=waving&color=gradient&customColorList=0,2,2,5,30&height=120&section=footer"/>

</div>
