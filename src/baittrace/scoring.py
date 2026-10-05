import hashlib
import math
import re
from datetime import datetime
from typing import Any, Dict, List, Set, Tuple

# --- Configurable Scoring Weights ---
WEIGHT_DISTINCT_VIDEOS: float = 30.0       # Strongest signal: cross-video coordination
WEIGHT_DISTINCT_AUTHORS: float = 20.0      # Sock-puppet shill network
WEIGHT_BURNER_AUTHORS: float = 15.0        # Bot/disposable channel profile
WEIGHT_LURE_DENSITY: float = 15.0          # Financial/job scam keyword prevalence
WEIGHT_NEAR_DUPLICATES: float = 15.0       # Copy-paste template reuse across videos
WEIGHT_TEMPORAL_BURST: float = 10.0        # Rapid comment posting burst (>=3 in 30 mins)
WEIGHT_INDICATOR_STRENGTH: float = 10.0    # Private invite link (+), Telegram handle (@), phone
WEIGHT_LLM_CONFIDENCE: float = 15.0        # Recruiter role confidence from Gemini

PENALTY_VICTIM_REPORT_MAJORITY: float = -45.0  # Comments are victims warning community
PENALTY_CREATOR_PROMO: float = -50.0          # Handle belongs to the video creator

# Promotion Score Cutoffs
THRESHOLD_CONFIRMED: float = 75.0
THRESHOLD_PROBABLE: float = 50.0
THRESHOLD_WATCH: float = 25.0

LURE_KEYWORDS = {
    "daily", "payment", "payout", "income", "earn", "salary", "bonus", "profit",
    "part time", "work from home", "task", "rating", "crypto", "betting",
    "deposit", "withdrawal", "guaranteed", "invest", "investment", "return",
    "free", "vip", "loot", "without investment", "easy money"
}

# --- 64-bit Simhash for Near-Duplicate Template Clustering ---

def _hash64(token: str) -> int:
    return int(hashlib.md5(token.encode('utf-8')).hexdigest()[:16], 16)

def compute_simhash(text: str) -> int:
    """Computes a 64-bit Simhash fingerprint for near-duplicate comment detection
    using dense character 4-grams and words.
    """
    cleaned = re.sub(r"[^\w\s]", "", text.lower()).strip()
    if not cleaned:
        return 0
    
    # Dense character 4-grams provide fine-grained similarity for short comments
    shingles = [cleaned[i:i+4] for i in range(len(cleaned) - 3)] if len(cleaned) >= 4 else [cleaned]
    words = cleaned.split()
    tokens = shingles + words
    
    v = [0] * 64
    for token in tokens:
        h = _hash64(token)
        for i in range(64):
            bit = (h >> i) & 1
            v[i] += 1 if bit else -1
            
    fingerprint = 0
    for i in range(64):
        if v[i] > 0:
            fingerprint |= (1 << i)
    return fingerprint

def hamming_distance(h1: int, h2: int) -> int:
    """Returns number of differing bits between two 64-bit hashes."""
    return bin(h1 ^ h2).count("1")

def find_duplicate_clusters(sightings: List[Dict[str, Any]], max_distance: int = 3) -> int:
    """Clusters sighting comments based on Simhash Hamming distance <= max_distance.
    Returns the size of the largest near-duplicate template cluster.
    """
    if len(sightings) < 2:
        return 1 if sightings else 0

    hashes = []
    for s in sightings:
        txt = s.get("comment_text", s.get("raw_comment", ""))
        h = compute_simhash(txt)
        if h != 0:
            hashes.append(h)

    if not hashes:
        return 0

    max_cluster = 1
    for i in range(len(hashes)):
        cluster = 1
        for j in range(len(hashes)):
            if i != j and hamming_distance(hashes[i], hashes[j]) <= max_distance:
                cluster += 1
        if cluster > max_cluster:
            max_cluster = cluster
    return max_cluster


# --- Burner Channel Analysis ---

def author_burner_score(author: str, channel_meta: Dict[str, Any] = None) -> float:
    """Evaluates how likely an author channel is an automated/burner sock-puppet account.
    Returns a score between 0.0 (established creator) and 1.0 (disposable burner).
    """
    score = 0.0
    meta = channel_meta or {}
    author_name = author or ""

    # 1. YouTube Auto-generated / Spam handle pattern:
    # e.g. @user-alnum5, @john12345, user_998877
    if re.search(r"@user-[a-z0-9]{4,}", author_name.lower()) or re.search(r"^[a-zA-Z_\.\-]+\d{4,}$", author_name.strip("@")):
        score += 0.35

    # 2. Uploads / Videos count
    video_count = meta.get("video_count")
    if video_count is not None:
        if video_count == 0:
            score += 0.25
        elif video_count <= 2:
            score += 0.10

    # 3. Subscriber count
    sub_count = meta.get("subscriber_count")
    if sub_count is not None:
        if sub_count == 0:
            score += 0.25
        elif sub_count < 10:
            score += 0.15

    # 4. Lack of channel description
    desc = meta.get("description", "")
    if not desc or len(desc.strip()) == 0:
        score += 0.10

    return min(1.0, max(0.0, score))


# --- Campaign-Level Evidence Scoring ---

def compute_campaign_score(handle_norm: str, sightings: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Evaluates all sightings for a single handle across YouTube to yield an authoritative
    campaign score (0-100) and promotion tier (CONFIRMED, PROBABLE, WATCH, DISCARD).
    """
    if not sightings:
        return {
            "handle": handle_norm,
            "campaign_score": 0.0,
            "tier": "DISCARD",
            "status": "SUPPRESSED",
            "evidence": {}
        }

    distinct_videos: Set[str] = set()
    distinct_authors: Set[str] = set()
    roles: List[str] = []
    confidences: List[float] = []
    lure_match_count = 0
    total_comments = len(sightings)
    is_creator_handle = False
    timestamp_list: List[datetime] = []

    for s in sightings:
        vid = s.get("video_id")
        if vid:
            distinct_videos.add(vid)
        
        auth = s.get("author")
        if auth:
            distinct_authors.add(auth)

        role = s.get("llm_role", "NEUTRAL").upper()
        roles.append(role)

        if role == "RECRUITER":
            confidences.append(s.get("llm_confidence", 0.85))

        comment_txt = s.get("comment_text", s.get("raw_comment", "")).lower()
        if any(keyword in comment_txt for keyword in LURE_KEYWORDS):
            lure_match_count += 1

        if s.get("is_creator_author", False):
            is_creator_handle = True

        # Parse timestamp if present
        time_val = s.get("posted_time")
        if isinstance(time_val, datetime):
            timestamp_list.append(time_val)
        elif isinstance(time_val, str) and time_val:
            try:
                dt = datetime.fromisoformat(time_val.replace("Z", "+00:00"))
                timestamp_list.append(dt)
            except Exception:
                pass

    # 1. Distinct Videos Score (log-scaled)
    # 1 video: 0.2 * WEIGHT, 2: 0.6 * WEIGHT, >=3: 1.0 * WEIGHT
    n_vids = len(distinct_videos)
    if n_vids <= 1:
        vids_score = WEIGHT_DISTINCT_VIDEOS * 0.25
    elif n_vids == 2:
        vids_score = WEIGHT_DISTINCT_VIDEOS * 0.65
    else:
        vids_score = WEIGHT_DISTINCT_VIDEOS * min(1.0, 0.65 + 0.15 * (n_vids - 2))

    # 2. Distinct Authors Score
    n_auth = len(distinct_authors)
    if n_auth <= 1:
        auth_score = WEIGHT_DISTINCT_AUTHORS * 0.25
    elif n_auth == 2:
        auth_score = WEIGHT_DISTINCT_AUTHORS * 0.65
    else:
        auth_score = WEIGHT_DISTINCT_AUTHORS * min(1.0, 0.65 + 0.15 * (n_auth - 2))

    # 3. Burner Author Score
    burner_scores = [
        author_burner_score(s.get("author", ""), s.get("channel_meta", {}))
        for s in sightings
    ]
    mean_burner = sum(burner_scores) / len(burner_scores) if burner_scores else 0.0
    burner_component = WEIGHT_BURNER_AUTHORS * mean_burner

    # 4. Lure Token Density
    lure_density = (lure_match_count / total_comments) if total_comments > 0 else 0.0
    lure_component = WEIGHT_LURE_DENSITY * lure_density

    # 5. Near Duplicate Template Cluster Size
    cluster_size = find_duplicate_clusters(sightings)
    if cluster_size >= 3:
        dup_component = WEIGHT_NEAR_DUPLICATES * 1.0
    elif cluster_size == 2:
        dup_component = WEIGHT_NEAR_DUPLICATES * 0.6
    else:
        dup_component = 0.0

    # 6. Temporal Burst (>=3 sightings within 30-min window)
    temporal_burst = False
    if len(timestamp_list) >= 3:
        timestamp_list.sort()
        for i in range(len(timestamp_list) - 2):
            if (timestamp_list[i + 2] - timestamp_list[i]).total_seconds() <= 1800:
                temporal_burst = True
                break
    burst_component = WEIGHT_TEMPORAL_BURST if temporal_burst else 0.0

    # 7. Indicator Strength
    if handle_norm.startswith("+"):
        indicator_factor = 1.0  # Private telegram invite link (+invite)
    elif handle_norm.startswith("@"):
        indicator_factor = 0.8  # Telegram handle
    elif "@" in handle_norm:
        indicator_factor = 0.9  # UPI handle
    else:
        indicator_factor = 0.6  # Phone number
    indicator_component = WEIGHT_INDICATOR_STRENGTH * indicator_factor

    # 8. Mean LLM Confidence for RECRUITER sightings
    mean_llm_conf = (sum(confidences) / len(confidences)) if confidences else 0.0
    llm_component = WEIGHT_LLM_CONFIDENCE * mean_llm_conf

    # Base additive score
    raw_score = (
        vids_score +
        auth_score +
        burner_component +
        lure_component +
        dup_component +
        burst_component +
        indicator_component +
        llm_component
    )

    # Penalties
    victim_report_count = roles.count("VICTIM_REPORT")
    recruiter_count = roles.count("RECRUITER")
    is_victim_majority = (victim_report_count > (total_comments / 2.0))

    if is_victim_majority:
        raw_score += PENALTY_VICTIM_REPORT_MAJORITY

    if is_creator_handle:
        raw_score += PENALTY_CREATOR_PROMO

    final_score = max(0.0, min(100.0, raw_score))

    # --- Promotion Rules ---
    # Rule 1: A single-sighting handle must NEVER reach CONFIRMED on LLM opinion alone.
    # Rule 2: CONFIRMED if score >= 75 OR (>=2 distinct videos AND >=2 distinct authors AND any RECRUITER verdict)
    is_multi_sighting = (n_vids >= 2 and n_auth >= 2)
    has_recruiter = (recruiter_count >= 1)

    if total_comments <= 1 or not is_multi_sighting:
        # Single sighting or single video/author
        if final_score >= THRESHOLD_PROBABLE and has_recruiter and not is_victim_majority:
            tier = "PROBABLE"
            status = "NEEDS_REVIEW"
        elif final_score >= THRESHOLD_WATCH:
            tier = "WATCH"
            status = "ACTIVE"
        else:
            tier = "DISCARD"
            status = "SUPPRESSED"
    else:
        # Multi-sighting campaign
        if not is_victim_majority and not is_creator_handle and (final_score >= THRESHOLD_CONFIRMED or (is_multi_sighting and has_recruiter)):
            tier = "CONFIRMED"
            status = "CONFIRMED"
        elif final_score >= THRESHOLD_PROBABLE:
            tier = "PROBABLE"
            status = "NEEDS_REVIEW"
        elif final_score >= THRESHOLD_WATCH:
            tier = "WATCH"
            status = "ACTIVE"
        else:
            tier = "DISCARD"
            status = "SUPPRESSED"

    return {
        "handle": handle_norm,
        "campaign_score": round(final_score, 2),
        "tier": tier,
        "status": status,
        "distinct_video_count": n_vids,
        "distinct_author_count": n_auth,
        "total_sightings": total_comments,
        "evidence": {
            "vids_score": round(vids_score, 2),
            "auth_score": round(auth_score, 2),
            "mean_burner": round(mean_burner, 2),
            "lure_density": round(lure_density, 2),
            "cluster_size": cluster_size,
            "temporal_burst": temporal_burst,
            "mean_llm_conf": round(mean_llm_conf, 2),
            "roles_summary": {
                "RECRUITER": recruiter_count,
                "VICTIM_REPORT": victim_report_count,
                "NEUTRAL": roles.count("NEUTRAL")
            }
        }
    }
