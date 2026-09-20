import pytest
from parser import extract_indicators, normalize_text

BENIGN_TEST_CASES = [
    # Confirmed false positives from original pipeline
    "Please call me on the telephone whenever free",
    "I saw this news on national television yesterday",
    "We played MTG option cards in the tournament",
    "Join the official telegram channel for more updates",
    
    # Vocabulary containing 'tele' stem
    "India telecom sector is growing rapidly in 2026",
    "Sending a message via telegraph is an old tradition",
    "Book an appointment for tele-consultation with doctor",
    "The teleprompter stopped working during speech",
    "We need telescope to observe the distant galaxy",
    "Teleportation is still a science fiction concept",
    
    # Generic emails (not UPI VPAs)
    "Contact our customer care at support@mycompany.com",
    "Send resume to user@protonmail.com for job inquiries",
    "My email is venky.dev@gmail.com for feedback",
    "Official inquiries: press@corporate.org",
    "Reach out at contact@outlook.com",
    
    # Prices / Currency amounts
    "The course fee is ₹9876543210 which is too high",
    "I bought this laptop for rs 9876543210 online",
    "Total bill came out to $9876543210 in dollars",
    "Refund of rs. 9876543210 was processed yesterday",
    
    # View counts, subscribers, likes
    "This video reached 9876543210 views in one week",
    "The creator now has 9876543210 subs on their channel",
    "Can we get 9876543210 likes on this video guys",
    "Total subscriber count is 9123456789 subscribers",
    
    # Repetitive / Fake phone numbers
    "Call 9999999999 if you need help",
    "My roll number is 9000000000 in university",
    "Test pattern: 9111111111",
    
    # Timestamps & Years
    "Uploaded at 2026-09-20 17:30:00 timestamp",
    "This was released back in 1998 and remastered in 2024",
    
    # Incidental "tg" or "tele" without handle/sigil
    "Playing tg with friends after school",
    "The tg value was measured in laboratory"
]

TRUE_POSITIVE_TEST_CASES = [
    # Telegram Links & Formats
    ("Join my group https://t.me/earn_daily_cash now", "TELEGRAM", "@earn_daily_cash"),
    ("Direct link: t.me/crypto_pump_signals daily profits", "TELEGRAM", "@crypto_pump_signals"),
    ("t.me/joinchat/+AbCdEfGh12345 private invite", "TELEGRAM", "+AbCdEfGh12345"),
    ("telegram: @task_payouts_bot get paid", "TELEGRAM", "@task_payouts_bot"),
    ("Contact on tg: @fast_money_2026", "TELEGRAM", "@fast_money_2026"),
    ("Reach me via tg @loot_deals_hub", "TELEGRAM", "@loot_deals_hub"),
    ("Connect on telegram -> loot_earning_app", "TELEGRAM", "@loot_earning_app"),
    ("Work available tele-gram: @daily_part_time", "TELEGRAM", "@daily_part_time"),
    
    # WhatsApp Links & Mentions
    ("Message me on wa.me/919876543210 for work", "WHATSAPP", "+919876543210"),
    ("Contact whatsapp 9876543210 for tasks", "WHATSAPP", "+919876543210"),
    ("Send message to wsp: +919123456780", "WHATSAPP", "+919123456780"),
    ("Chat on wa: 9988776655 daily income", "WHATSAPP", "+919988776655"),
    
    # Indian Phone Numbers (Valid)
    ("Call manager at 9876543210 immediately", "PHONE", "+919876543210"),
    ("Helpline number +91 9123456789 open 24/7", "PHONE", "+919123456789"),
    
    # UPI VPAs (Whitelisted PSPs)
    ("Send registration fee to scammer@okaxis now", "UPI", "scammer@okaxis"),
    ("Pay processing charges to merchant123@paytm", "UPI", "merchant123@paytm"),
    ("Deposit amount to investment@oksbi for returns", "UPI", "investment@oksbi"),
    ("UPI id: trader.pro@ybl send screenshot", "UPI", "trader.pro@ybl")
]


@pytest.mark.parametrize("text", BENIGN_TEST_CASES)
def test_benign_text_produces_zero_indicators(text):
    cleaned = normalize_text(text)
    indicators = extract_indicators(cleaned)
    assert len(indicators) == 0, (
        f"False positive detected! Text: '{text}' produced indicators: "
        f"{[(i.indicator_type, i.normalized_value) for i in indicators]}"
    )


@pytest.mark.parametrize("text,expected_type,expected_val", TRUE_POSITIVE_TEST_CASES)
def test_true_positive_produces_indicator(text, expected_type, expected_val):
    cleaned = normalize_text(text)
    indicators = extract_indicators(cleaned)
    assert len(indicators) >= 1, f"Expected indicator from '{text}', got 0"
    
    matches = [
        ind for ind in indicators 
        if ind.indicator_type == expected_type and ind.normalized_value.lower() == expected_val.lower()
    ]
    assert len(matches) >= 1, (
        f"Expected {expected_type}:{expected_val}, found: "
        f"{[(i.indicator_type, i.normalized_value) for i in indicators]}"
    )
