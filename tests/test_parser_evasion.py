import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from baittrace.parser import extract_indicators, normalize_text

def test_evasion():
    test_cases = [
        {
            "text": "Join our daily payment channel: https://t.me/+v5X9L1mQz",
            "expected_normalized": "+v5X9L1mQz",
            "indicator_type": "TELEGRAM"
        },
        {
            "text": "Contact HR on t [dot] me/task_payout_2026 for daily payout",
            "expected_normalized": "@task_payout_2026",
            "indicator_type": "TELEGRAM"
        },
        {
            "text": "Message on Te1egram: @ daily_income_hub",
            "expected_normalized": "@daily_income_hub",
            "indicator_type": "TELEGRAM"
        },
        {
            "text": "Earn Rs 2000 daily whatsapp 9 8 7 6 5 4 3 2 1 0 for tasks",
            "expected_normalized": "+919876543210",
            "indicator_type": "WHATSAPP"
        },
        {
            "text": "This video was very helpful, thank you!",
            "expected_normalized": None,
            "indicator_type": None
        }
    ]

    all_passed = True
    for i, tc in enumerate(test_cases, 1):
        cleaned = normalize_text(tc["text"])
        indicators = extract_indicators(cleaned)
        
        if tc["expected_normalized"] is None:
            if len(indicators) != 0:
                print(f"Test {i} FAILED: Expected 0 indicators, got {len(indicators)}")
                all_passed = False
            else:
                print(f"Test {i} PASSED")
            continue
            
        if not indicators:
            print(f"Test {i} FAILED: Expected indicator, got none. Cleaned: '{cleaned}'")
            all_passed = False
            continue
            
        match_found = False
        for ind in indicators:
            if ind.normalized_value == tc["expected_normalized"] and ind.indicator_type == tc["indicator_type"]:
                match_found = True
                break
        
        if match_found:
            print(f"Test {i} PASSED")
        else:
            print(f"Test {i} FAILED: Did not find {tc['indicator_type']} {tc['expected_normalized']}. Found: {[(ind.indicator_type, ind.normalized_value) for ind in indicators]}")
            all_passed = False

    if not all_passed:
        assert False, "One or more evasion test cases failed"
    else:
        print("All tests passed.")

if __name__ == '__main__':
    test_evasion()
