"""Dania-only locked matching -> fixed follow-ups -> placeholder recommendation.

The caller provides ask_answer, so Sarah can connect her own STT and speech
functions without copying them into this file. Standard library only.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping


@dataclass(frozen=True)
class Question:
    field: str
    arabic: str


@dataclass(frozen=True)
class Protocol:
    name: str
    priority: int
    required_symptoms: tuple[str, ...]
    followups: tuple[Question, ...]


def q(field: str, arabic: str) -> Question:
    return Question(field, arabic)


# Document order resolves ties at the same priority. This is deterministic but
# does not imply a clinical ordering between conditions with the same rank.
PROTOCOLS: tuple[Protocol, ...] = (
    Protocol("cardiac_arrest", 1, ("cardiac_arrest",), (
        q("is_breathing", "هل المريض يتنفس حالياً؟"),
        q("cpr_started", "هل بدأ حد بعمل إنعاش قلبي؟"),
        q("duration_mentioned", "من إمتى توقف قلبه؟"),
    )),
    Protocol("choking", 1, ("choking",), (
        q("is_breathing", "هل المريض يقدر يتنفس أو يسعل؟"),
        q("object_visible", "هل الجسم الغريب ظاهر بالفم؟"),
        q("patient_status", "هل المريض واعي ولا فاقد الوعي؟"),
    )),
    Protocol("unconscious", 2, ("unconscious",), (
        q("is_breathing", "هل المريض يتنفس حالياً؟"),
        q("duration_mentioned", "من إمتى فاقد الوعي؟"),
        q("cause_mentioned", "شو صار قبل ما يفقد وعيه؟"),
    )),
    Protocol("drowning", 2, ("drowning",), (
        q("is_breathing", "هل المريض يتنفس حالياً؟"),
        q("patient_status", "هل المريض واعي ولا فاقد الوعي؟"),
        q("duration_mentioned", "قديش المدة اللي ضل فيها تحت الماء؟"),
    )),
    Protocol("electric_shock", 3, ("electric_shock",), (
        q("is_breathing", "هل المريض يتنفس حالياً؟"),
        q("patient_status", "هل المريض واعي ولا فاقد الوعي؟"),
        q("source_removed", "هل تم فصل مصدر الكهرباء عن المصاب؟"),
    )),
    Protocol("stroke_symptoms", 3, ("stroke_symptoms",), (
        q("duration_mentioned", "من إمتى بدأت الأعراض بالضبط؟"),
        q("face_weakness", "هل يوجد تدلي أو ضعف بجانب من الوجه؟"),
        q("speech_difficulty", "هل يعاني من صعوبة بالكلام؟"),
    )),
    Protocol("head_injury", 4, ("head_injury",), (
        q("patient_status", "هل المريض واعي ولا فاقد الوعي؟"),
        q("bleeding_severity", "هل يوجد نزيف من الرأس؟ شديد ولا خفيف؟"),
        q("cause_mentioned", "كيف صارت الإصابة؟ وقعة ولا ضربة؟"),
    )),
    Protocol("allergic_reaction", 4, ("allergic_reaction",), (
        q("is_breathing", "هل يعاني من صعوبة بالتنفس؟"),
        q("cause_mentioned", "شو سبب الحساسية؟ أكل، دواء، ولا لدغة؟"),
        q("swelling", "هل يوجد تورم بالوجه أو الحلق؟"),
    )),
    Protocol("bleeding", 5, ("bleeding",), (
        q("bleeding_severity", "هل النزيف شديد ولا خفيف؟"),
        q("bleeding_location", "وين مكان النزيف بالضبط؟"),
        q("bleeding_ongoing", "هل النزيف متوقف ولا لسا مستمر؟"),
    )),
    Protocol("seizure", 6, ("seizure",), (
        q("seizure_ongoing", "هل التشنج لسا مستمر ولا وقف؟"),
        q("duration_mentioned", "قديش استمر التشنج؟"),
        q("seizure_history", "هل عنده تاريخ مرضي بالصرع؟"),
    )),
    Protocol("burn", 7, ("burn",), (
        q("burn_cause", "شو سبب الحرق؟ نار، كهرباء، ولا مواد كيميائية؟"),
        q("burn_size", "قديش مساحة منطقة الحرق تقريباً؟"),
        q("burn_location", "وين مكان الحرق بالجسم؟"),
    )),
    Protocol("fracture", 8, ("fracture",), (
        q("fracture_location", "وين مكان الكسر بالضبط؟"),
        q("fracture_open", "هل العظم طالع من الجلد ولا الكسر مغلق؟"),
        q("cause_mentioned", "كيف صار الكسر؟ وقعة ولا ضربة؟"),
    )),
    Protocol("abdominal_pain", 10, ("abdominal_pain",), (
        q("pain_location", "وين بالضبط مكان الألم بالبطن؟"),
        q("duration_mentioned", "من إمتى بدأ الألم؟"),
        q("pain_severity", "هل الألم شديد جداً ولا يحتمل؟"),
    )),
    Protocol("vomiting", 11, ("vomiting",), (
        q("vomit_content", "هل يوجد دم بالقيء؟"),
        q("duration_mentioned", "من إمتى بدأ التقيؤ؟"),
        q("associated_symptom", "هل فيه ألم بالبطن مع التقيؤ؟"),
    )),
)

SYMPTOM_KEYS = tuple(p.name for p in PROTOCOLS)
PROTOCOL_FIELDS = tuple(dict.fromkeys(q.field for p in PROTOCOLS for q in p.followups))


def answer_field(protocol: Protocol, field: str) -> str:
    """Use separate storage for questions with different clinical meanings."""
    if field == "patient_status":
        return "consciousness_status"
    if protocol.name == "allergic_reaction" and field == "is_breathing":
        return "breathing_difficulty"
    if protocol.name == "choking" and field == "is_breathing":
        return "can_breathe_or_cough"
    return field


ANSWER_FIELDS = tuple(dict.fromkeys(answer_field(p, q.field) for p in PROTOCOLS for q in p.followups))

# Candidate guidance for a trained responder. Keep it separate from matching
# and have the responsible service approve it against its local EMS protocol.
AHA_FIRST_AID = "https://cpr.heart.org/en/resuscitation-science/2024-first-aid-guidelines"
AHA_BLS = "https://cpr.heart.org/en/resuscitation-science/cpr-and-ecc-guidelines/adult-basic-life-support"
WHO_PREHOSPITAL = "https://www.who.int/teams/integrated-health-services/clinical-services-and-systems/emergency-and-critical-care/prehospital-toolkit"
RED_CROSS_DROWNING = "https://guidelines.redcross.org/guidelines-database/drowning-process-resuscitation/"
RED_CROSS_BURNS = "https://www.redcross.org/take-a-class/resources/learn-first-aid/burns"

ACTION_CARDS = {
    "cardiac_arrest": [
        ("قيّم الاستجابة والتنفس الطبيعي والنبض سريعًا؛ إذا تأكد توقف القلب، ابدأ الإنعاش القلبي الرئوي فورًا بحسب عمر المريض وتدريبك.", AHA_BLS),
        ("أحضر مزيل الرجفان الآلي واستخدمه فور توفره واتبع تعليماته، مع تقليل انقطاع الضغطات.", AHA_BLS),
        ("استمر بإعادة تقييم التنفس والنبض واتبع خوارزمية الإنعاش المعتمدة لدى فريقك.", AHA_BLS),
    ],
    "choking": [
        ("إذا كان يسعل بقوة ويستطيع التنفس والكلام، شجعه على السعال وراقب ظهور انسداد شديد.", AHA_BLS),
        ("إذا كان بالغًا واعيًا مع انسداد شديد، اتبع خوارزمية العمر المناسبة؛ للبالغين خمس ضربات للظهر ثم خمس ضغطات بطنية بالتناوب.", AHA_BLS),
        ("إذا فقد الاستجابة، ابدأ خوارزمية الإنعاش المناسبة للعمر؛ افحص الفم بحثًا عن جسم ظاهر قبل محاولات التنفس ولا تُدخل إصبعك عشوائيًا.", AHA_BLS),
    ],
    "unconscious": [
        ("افتح مجرى الهواء وقيّم التنفس والنبض بانتظام؛ إذا غاب التنفس الطبيعي أو النبض اتبع خوارزمية الإنعاش المناسبة.", WHO_PREHOSPITAL),
        ("إذا كان يتنفس ولا يُشتبه بإصابة تمنع تدويره، ضعه على جانبه لحماية مجرى الهواء مع مراقبة التنفس المستمرة.", AHA_FIRST_AID),
        ("افحص سكر الدم إذا توفر، وابحث عن سبب قابل للعلاج لتغير الوعي وفق بروتوكول الفريق.", WHO_PREHOSPITAL),
    ],
    "drowning": [
        ("أنقذ المريض دون تعريض الفريق للخطر، ثم افتح مجرى الهواء وقيّم التنفس والنبض بعد إخراجه من الماء.", RED_CROSS_DROWNING),
        ("إذا تأكد توقف القلب بعد الغرق، ابدأ إنعاش الغرق المناسب للعمر مع إعطاء أولوية للتهوية بحسب تدريب الفريق.", RED_CROSS_DROWNING),
        ("أعد تقييم التنفس وحرارة الجسم أثناء النقل؛ لا تستخدم مزيل الرجفان والمريض ما زال في الماء.", RED_CROSS_DROWNING),
    ],
    "electric_shock": [
        ("لا تلمس المصاب حتى تتأكد من فصل مصدر الكهرباء وسلامة المكان.", RED_CROSS_BURNS),
        ("بعد تأمين المكان، افحص الاستجابة والتنفس والنبض وابدأ الإنعاش إذا تأكد التوقف.", WHO_PREHOSPITAL),
        ("افحص مواضع الحروق والإصابات المصاحبة وراقب تخطيط القلب إن توفر وفق بروتوكول الفريق.", WHO_PREHOSPITAL),
    ],
    "stroke_symptoms": [
        ("افحص تدلي الوجه وضعف الذراع وتغير الكلام وحدد وقت آخر مرة كان المريض فيها طبيعيًا.", AHA_FIRST_AID),
        ("افحص سكر الدم إذا توفر دون تأخير رعاية السكتة؛ نقص السكر قد يشبه أعراضها.", AHA_FIRST_AID),
        ("راقب الوعي والتنفس وتغير الأعراض أثناء النقل واتبع مسار السكتة لدى فريقك.", WHO_PREHOSPITAL),
    ],
    "head_injury": [
        ("ثبّت الرأس والرقبة إذا دلت آلية الإصابة أو الفحص على خطر إصابة العمود الفقري، مع الحفاظ على مجرى الهواء.", WHO_PREHOSPITAL),
        ("أعد تقييم الوعي والحدقتين والتنفس والقيء والتشنجات بحثًا عن تدهور عصبي.", WHO_PREHOSPITAL),
        ("اضبط النزف الخارجي وفق بروتوكول إصابات الرأس وافحص وجود إصابات مرافقة.", WHO_PREHOSPITAL),
    ],
    "allergic_reaction": [
        ("قيّم مجرى الهواء وصعوبة التنفس وتورم اللسان أو الحلق والدورة الدموية؛ ابحث عن علامات التأق.", AHA_FIRST_AID),
        ("إذا اشتُبه بالتأق، أعطِ الأدرينالين العضلي فورًا وفق صلاحياتك وبروتوكول الفريق المعتمد، ولا تؤخره انتظارًا لمضاد الهيستامين.", WHO_PREHOSPITAL),
        ("أعد تقييم التنفس والضغط والاستجابة بعد العلاج، وجهّز دعم مجرى الهواء إذا ساءت الحالة.", WHO_PREHOSPITAL),
    ],
    "bleeding": [
        ("اضغط مباشرة على موضع النزف الخارجي الشديد واستمر حتى يتوقف أو تنتقل إلى وسيلة السيطرة التالية.", AHA_FIRST_AID),
        ("إذا بقي نزف طرفي مهدد للحياة رغم الضغط، استخدم رباطًا ضاغطًا مخصصًا إن توفر وكنت مدربًا عليه، وسجّل وقت وضعه.", AHA_FIRST_AID),
        ("أعد تقييم النزف والنبض والوعي وعلامات الصدمة خلال النقل.", WHO_PREHOSPITAL),
    ],
    "seizure": [
        ("أبعد الأشياء المؤذية واحمِ الرأس، وابدأ توقيت التشنج؛ لا تقيّد المريض ولا تضع شيئًا في فمه.", AHA_FIRST_AID),
        ("راقب مجرى الهواء والتنفس؛ بعد توقف التشنج، ساعده على الوضع الجانبي إذا كان يتنفس وتسمح الإصابة بذلك.", AHA_FIRST_AID),
        ("إذا استمر أكثر من خمس دقائق أو تكرر دون عودة الوعي، اتبع بروتوكول النوبة المطولة والأدوية المصرح بها لفريقك.", AHA_FIRST_AID),
    ],
    "burn": [
        ("أوقف التعرض لمصدر الحرق بأمان. إذا كان الحرق حراريًا، برّده بماء جارٍ نظيف وفق بروتوكول الفريق وتجنب انخفاض حرارة الجسم.", AHA_FIRST_AID),
        ("افحص مساحة الحرق وعمقه ومكانه، وغطّه بضماد نظيف غير لاصق حسب البروتوكول.", WHO_PREHOSPITAL),
        ("إذا كان الحرق بالوجه أو وُجد دخان مستنشق أو صعوبة تنفس، أعد تقييم مجرى الهواء باستمرار.", WHO_PREHOSPITAL),
    ],
    "fracture": [
        ("افحص التروية والإحساس والحركة أسفل موضع الإصابة قبل التثبيت وبعده.", AHA_FIRST_AID),
        ("ثبّت الطرف المصاب في الوضع الذي وجدته وفق بروتوكول الفريق؛ غطِّ الجرح المفتوح بضماد نظيف.", AHA_FIRST_AID),
        ("إذا كان هناك نزف شديد أو طرف شاحب أو أزرق، عالج النزف وصعّد الحالة فورًا.", AHA_FIRST_AID),
    ],
    "abdominal_pain": [
        ("افحص البطن ومكان الألم وشدته وتطوره، وقس العلامات الحيوية وابحث عن علامات الصدمة.", WHO_PREHOSPITAL),
        ("لا تعطِ المريض طعامًا أو شرابًا إذا اشتُبه بسبب بطني حاد؛ قيّم احتمال الحمل عند من ينطبق عليها ذلك.", WHO_PREHOSPITAL),
        ("وفّر تسكينًا مناسبًا للغثيان والألم فقط وفق صلاحياتك وبروتوكول الفريق، مع إعادة التقييم.", WHO_PREHOSPITAL),
    ],
    "vomiting": [
        ("احمِ مجرى الهواء؛ إذا قل الوعي وكان يتنفس، ضعه على جانبه عندما تسمح الإصابة بذلك، واستخدم الشفط إذا لزم وكنت مدربًا.", WHO_PREHOSPITAL),
        ("قيّم تكرار القيء ووجود دم وعلامات الجفاف أو الصدمة، وراقب العلامات الحيوية.", WHO_PREHOSPITAL),
        ("إذا لزم دواء مضاد للقيء، استخدمه فقط وفق صلاحياتك وبروتوكول الفريق مع إعادة التقييم.", WHO_PREHOSPITAL),
    ],
}


def match_protocol(data: Mapping[str, Any]) -> Protocol | None:
    """Return the first matching protocol by priority and locked table order."""
    symptoms = data.get("symptoms") or []
    if not isinstance(symptoms, (list, tuple, set, frozenset)):
        raise ValueError("symptoms must be a list of symptom keys")
    present = set(symptoms)
    for protocol in PROTOCOLS:
        if set(protocol.required_symptoms).issubset(present):
            return protocol
    return None


def missing_followups(protocol: Protocol | None, data: Mapping[str, Any]) -> list[Question]:
    """Only the selected protocol's unanswered fixed questions, in table order."""
    if protocol is None:
        return []
    def unanswered(question: Question) -> bool:
        key = answer_field(protocol, question.field)
        value = data.get(key)
        # Earlier test cases may explicitly encode consciousness in patient_status.
        if question.field == "patient_status" and value is None:
            value = data.get("patient_status")
            if value in ("alive", "deceased"):
                value = None
        return value is None or value == "" or value == "unknown"

    return [question for question in protocol.followups if unanswered(question)]


def process_case(data: Mapping[str, Any]) -> dict[str, Any]:
    """Stable, JSON-ready result for Sarah's pipeline and Sadeel's UI."""
    protocol = match_protocol(data)
    return {
        "matched_protocol": protocol.name if protocol else None,
        "priority": protocol.priority if protocol else None,
        "missing_followups": [
            {"field": question.field, "answer_field": answer_field(protocol, question.field),
             "question_ar": question.arabic}
            for question in missing_followups(protocol, data)
        ],
    }


def select_action_cards(name: str, case: Mapping[str, Any]) -> list[dict[str, str]]:
    """Remove actions ruled out by *explicit* answers; never infer a vital sign."""
    cards = ACTION_CARDS[name]
    if name == "bleeding":
        severity = str(case.get("bleeding_severity") or "").strip().casefold()
        ongoing = str(case.get("bleeding_ongoing") or "").strip().casefold()
        # A tourniquet is for uncontrolled, life-threatening extremity bleeding.
        if severity in {"mild", "light", "خفيف", "بسيط"} or ongoing in {
            "no", "false", "stopped", "لا", "توقف", "متوقف", "وقف"
        }:
            cards = [cards[0], cards[2]]
    return [{"text_ar": message, "source_url": source} for message, source in cards]


def run_protocol_flow(
    data: Mapping[str, Any],
    ask_answer: Callable[[str, str, dict[str, Any]], Any],
) -> dict[str, Any]:
    """Match, collect fixed questions, then produce the placeholder result.

    ask_answer receives (fixed_arabic_question, answer_field, current_case).
    It must return the answer for that one field, or None if unclear. A caller
    can implement it with terminal input or with Sarah's voice/STT system.
    Neither the initial match nor a recommendation is sent to the callback.
    """
    if not isinstance(data, Mapping):
        raise TypeError("data must be a JSON object/dict")
    case = dict(data)  # Never overwrite Sarah's original dict.
    initial = process_case(case)
    if initial["matched_protocol"] is None:
        return {"data": case, "matched_protocol": None, "priority": None,
                "asked_questions": [], "unanswered_fields": [],
                "recommendation": None, "suggested_actions": []}

    asked = []
    for item in initial["missing_followups"]:
        field = item["answer_field"]
        answer = ask_answer(item["question_ar"], field, dict(case))
        # Preserve false/zero, and never fill a field from an empty answer.
        if isinstance(answer, str):
            answer = answer.strip()
        if answer is not None and answer != "" and answer != "unknown":
            case[field] = answer
        value = case.get(field)
        asked.append({"field": field, "question_ar": item["question_ar"],
                      "answered": value is not None and value != "" and value != "unknown"})

    final = process_case(case)
    name, priority = final["matched_protocol"], final["priority"]
    # Construct these only after every fixed follow-up has been offered.
    suggestions = select_action_cards(name, case)
    match_summary = f"matched protocol: {name}, priority: {priority}"
    recommendation = "\n".join(item["text_ar"] for item in suggestions)
    return {
        "data": case,
        "matched_protocol": name,
        "priority": priority,
        "asked_questions": asked,
        "unanswered_fields": [item["answer_field"] for item in final["missing_followups"]],
        "match_summary": match_summary,
        "recommendation": recommendation,
        "suggested_actions": suggestions,
        "clinical_review_status": "draft_requires_local_protocol_approval",
    }


def update_protocol(previous: Mapping[str, Any], current: Mapping[str, Any]) -> dict[str, Any]:
    """Re-check only on the agreed meaningful changes; identify protocol swaps."""
    old = match_protocol(previous)
    old_name = old.name if old else None
    new_symptoms = set(current.get("symptoms") or []) - set(previous.get("symptoms") or [])
    triggered = bool(new_symptoms) or any(
        previous.get(field) != current.get(field)
        for field in ("is_breathing", "patient_status")
    ) or (not bool(previous.get("deteriorating")) and bool(current.get("deteriorating")))
    if not triggered:
        return {"rechecked": False, "changed": False, "previous_protocol": old_name,
                "matched_protocol": old_name, "priority": old.priority if old else None}
    result = process_case(current)
    return {"rechecked": True, "changed": old_name != result["matched_protocol"],
            "previous_protocol": old_name, **result}


def main() -> None:
    parser = argparse.ArgumentParser(description="Match a structured case JSON to the locked protocol table")
    parser.add_argument("case_json", nargs="?", type=Path,
                        help="JSON from Sarah; omit to try an example bleeding case")
    args = parser.parse_args()
    data = (json.loads(args.case_json.read_text(encoding="utf-8")) if args.case_json
            else {"patient_status": "alive", "symptoms": ["fracture", "bleeding"],
                  "bleeding_severity": "severe"})
    if not isinstance(data, dict):
        parser.error("case JSON must be an object")

    def ask_in_terminal(question_ar: str, field: str, current_case: dict[str, Any]) -> str:
        print(question_ar)
        return input("Answer (leave blank if unknown): ")

    outcome = run_protocol_flow(data, ask_in_terminal)
    print("\n" + (outcome.get("match_summary") or "No protocol matched"))
    if outcome["recommendation"]:
        print(outcome["recommendation"])
    print(json.dumps(outcome, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
