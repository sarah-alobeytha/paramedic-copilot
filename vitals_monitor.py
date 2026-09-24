"""Second phase: repeated vitals check-ins during transport.

Pass in your own speak() and ask() so this file never touches the microphone
or the models directly. Standard library only (plus Dania's module).
"""

from __future__ import annotations

import copy
import time
from datetime import datetime
from typing import Any, Callable

from dania_protocol_flow import select_action_cards, update_protocol

VITALS_TABLE = [
    ("blood_pressure", "شو ضغط الدم حالياً؟"),
    ("heart_rate", "شو معدل ضربات القلب؟"),
    ("temperature", "شو درجة الحرارة؟"),
    ("oxygen_saturation", "شو نسبة الأكسجين بالدم؟"),
    ("status_change", "هل صار في أي تغيير بحالة المريض من آخر مرة؟"),
]
BREATHING_Q = "هل المريض يتنفس حالياً؟"


def _clean(value: Any) -> Any:
    """Turn empty/unknown answers into None."""
    if isinstance(value, str):
        value = value.strip()
    return None if value in (None, "", "unknown") else value


def _is_positive(value: Any) -> bool:
    """status_change counts as a change unless clearly 'no'. Unknown is not a change."""
    return value is not None and str(value).strip().casefold() not in {"no", "none", "false", "لا"}


def run_vitals_monitor(
    case: dict[str, Any],
    ask: Callable[[str, str], Any],
    speak: Callable[[str], None],
    interval_s: int = 120,
    max_checks: int | None = None,
) -> list[dict[str, Any]]:
    """Repeat vitals check-ins until Ctrl+C (or max_checks). Mutates `case`.

    ask(question_ar, field) -> answer string, or "unknown".
    Each check-in is timestamped and appended to case["vitals_timeline"];
    it is never merged into the static intake fields.
    """
    timeline: list[dict[str, Any]] = case.setdefault("vitals_timeline", [])
    checks = 0
    try:
        while max_checks is None or checks < max_checks:
            print(f"\nNext vitals check in {interval_s}s (Ctrl+C to finish)...")
            time.sleep(interval_s)
            checks += 1
            previous = copy.deepcopy(case)
            entry: dict[str, Any] = {"timestamp": datetime.now().isoformat(timespec="seconds")}

            # Breathing is safety-critical: one retry if the answer was unclear.
            breathing = _clean(ask(BREATHING_Q, "is_breathing"))
            if breathing is None:
                breathing = _clean(ask(BREATHING_Q, "is_breathing"))
            entry["is_breathing"] = breathing
            case["is_breathing"] = breathing or "unknown"

            for field, question in VITALS_TABLE:
                entry[field] = _clean(ask(question, field))

            # Deterioration rule: status change reported, or breathing yes -> no/unknown.
            was_breathing = str(previous.get("is_breathing") or "").casefold() == "yes"
            now_breathing = str(breathing or "").casefold() == "yes"
            if (was_breathing and not now_breathing) or _is_positive(entry["status_change"]):
                case["deteriorating"] = True
            entry["deteriorating"] = bool(case.get("deteriorating"))
            timeline.append(entry)

            if case.get("deteriorating") and not previous.get("deteriorating"):
                print(">> DETERIORATING")
                speak("تنبيه: حالة المريض تتدهور.")

            # Re-match only on the agreed triggers (handled inside update_protocol).
            result = update_protocol(previous, case)
            if result["changed"]:
                new_name = result["matched_protocol"]
                print(f">> RECOMMENDATION UPDATED: {result['previous_protocol']} -> {new_name}")
                case.setdefault("protocol_history", []).append({
                    "timestamp": entry["timestamp"],
                    "from": result["previous_protocol"],
                    "to": new_name,
                })
                speak("تم تحديث التوصية.")
                if new_name:
                    for card in select_action_cards(new_name, case):
                        print(card["text_ar"])
                        speak(card["text_ar"])
    except KeyboardInterrupt:
        print("\nMonitoring stopped.")
    return timeline
