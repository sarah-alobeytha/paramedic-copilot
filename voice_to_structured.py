import sounddevice as sd
import soundfile as sf
import numpy as np
from transformers import pipeline, VitsModel, AutoTokenizer
from groq import Groq
import json
import torch
from dania_protocol_flow import run_protocol_flow
import re
from dania_protocol_flow import run_protocol_flow, SYMPTOM_KEYS
from vitals_monitor import run_vitals_monitor

print("Loading Whisper model...")
stt_pipe = pipeline("automatic-speech-recognition", model="oddadmix/whisper-large-v3-turbo-arabic-dialectal-v2")
print("Whisper ready.\n")

print("Loading TTS model...")
tts_model = VitsModel.from_pretrained("facebook/mms-tts-ara")
tts_tokenizer = AutoTokenizer.from_pretrained("facebook/mms-tts-ara")
print("TTS ready.\n")


groq_client = Groq(api_key="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")
SAMPLE_RATE = 16000




def record_audio():
    input("Press ENTER to start recording, then speak...")
    print("Recording... press ENTER again to stop.")
    frames = []
    def callback(indata, frame_count, time_info, status):
        frames.append(indata.copy())
    stream = sd.InputStream(samplerate=SAMPLE_RATE, channels=1, callback=callback)
    stream.start()
    input()
    stream.stop()
    stream.close()
    recording = np.concatenate(frames, axis=0)
    sf.write("recording.wav", recording, SAMPLE_RATE)
    print(f"Saved {len(recording)/SAMPLE_RATE:.1f} seconds.\n")
    return "recording.wav"

def transcribe(audio_file):
    return stt_pipe(audio_file)["text"]

from transformers import VitsModel, AutoTokenizer
import torch

print("Loading TTS model...")
tts_model = VitsModel.from_pretrained("facebook/mms-tts-ara")
tts_tokenizer = AutoTokenizer.from_pretrained("facebook/mms-tts-ara")
print("TTS ready.\n")

def speak(text):
    """Generate speech audio from Arabic text and play it out loud, using local MMS-TTS."""
    inputs = tts_tokenizer(text, return_tensors="pt")
    with torch.no_grad():
        output = tts_model(**inputs).waveform

    audio_data = output.squeeze().numpy()
    sample_rate = tts_model.config.sampling_rate

    sf.write("question.wav", audio_data, sample_rate)
    data, sr = sf.read("question.wav")
    sd.play(data, sr)
    sd.wait()
def extract_structured(text):
    prompt = f"""You are a medical NLU extraction system for a paramedic voice assistant.
Given the following transcribed Arabic speech from a paramedic, extract structured information.

Transcribed text: "{text}"

You MUST choose symptoms ONLY from this exact list:
["bleeding", "fracture", "burn", "electric_shock", "seizure", "unconscious",
"vomiting", "abdominal_pain", "head_injury", "cardiac_arrest", "stroke_symptoms",
"allergic_reaction", "drowning", "choking"]

Severe chest pain or heart-attack-like symptoms should map to "cardiac_arrest".
If none fit, use "other" and describe it in cause_mentioned.

For age:
- Extract the patient's age if explicitly mentioned.
- Preserve approximate expressions such as "mid-thirties" if that is what was said.
- If age is not mentioned, use null.

For gender:
- Use "male" or "female" if explicitly stated or clearly indicated.
- If gender is not mentioned, use "unknown".

Return ONLY valid JSON, no markdown, no explanation:
{{
  "patient_status": "<alive/deceased/unknown>",
  "age": "<age if mentioned, else null>",
  "gender": "<male/female/unknown>",
  "symptoms": ["<from the list above>"],
  "cause_mentioned": "<cause if mentioned, else null>",
  "duration_mentioned": "<duration if mentioned, else null>",
  "confidence": "<high/medium/low>"
}}"""

    def call_groq(use_json_mode):
        kwargs = {
            "model": "openai/gpt-oss-120b",
            "max_tokens": 2000,
            "reasoning_effort": "low",
            "messages": [{"role": "user", "content": prompt}],
        }
        if use_json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        return groq_client.chat.completions.create(**kwargs)

    try:
        response = call_groq(use_json_mode=True)
    except Exception as e:
        print(f"!! json_object mode failed ({e}), retrying without it...")
        response = call_groq(use_json_mode=False)

    raw = response.choices[0].message.content
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        cleaned = cleaned.replace("json", "", 1).strip()

    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        print("!! extract_structured got bad JSON from the model. Raw output was:")
        print(repr(raw))
        return {
            "patient_status": "unknown",
            "age": None,
            "gender": "unknown",
            "symptoms": [],
            "cause_mentioned": None,
            "duration_mentioned": None,
            "confidence": "low"
        }


def generate_followup_question(missing_field, current_data):
    prompt = f"""You are a paramedic voice assistant. Based on this case data:
{json.dumps(current_data, ensure_ascii=False)}

The field "{missing_field}" is missing. Generate ONE short, natural follow-up
question in Arabic (Jordanian dialect) asking for exactly that information.
Return ONLY the question text."""
    response = groq_client.chat.completions.create(
        model="openai/gpt-oss-120b",
        max_tokens=100,
        messages=[{"role": "user", "content": prompt}]
    )
    return response.choices[0].message.content.strip()


def merge_data(base, new):
    for key, value in new.items():
        if key == "symptoms":
            if value:
                combined = list(dict.fromkeys(base.get("symptoms", []) + value))
                if len(combined) > 1 and "other" in combined:
                    combined.remove("other")
                base["symptoms"] = combined
        elif value not in [None, "", [], "unknown"]:
            base[key] = value
    return base


def interpret_answer(field, question_ar, raw_text):
    prompt = f"""A paramedic was asked (Arabic): "{question_ar}"
Field: "{field}". Their spoken answer: "{raw_text}"

Return ONLY a short value. For yes/no questions return "yes" or "no".
For bleeding_ongoing return "ongoing" or "stopped".
Only use what is explicitly said. If the text is garbled or unclear, return "unknown". Never guess.
If unclear or off-topic, return "unknown"
For blood pressure, heart rate, temperature and oxygen saturation, return only the number(s), e.g. 120/80 or 98.."""
    try:
        r = groq_client.chat.completions.create(
            model="openai/gpt-oss-120b", max_tokens=200,
            messages=[{"role": "user", "content": prompt}])
        return (r.choices[0].message.content or "").strip() or "unknown"
    except Exception as e:
        print(f"!! interpret_answer failed: {e}")
        return "unknown"

def ask_dania_question(question_ar, field, current_case):
    speak(question_ar)
    raw = transcribe(record_audio())
    print(f"Paramedic answered ({field}):", raw, "\n")
    return interpret_answer(field, question_ar, raw)

# ---- Main flow ----
audio_file = record_audio()
text = transcribe(audio_file)
print("Transcribed text:", text, "\n")

data = extract_structured(text)
print("Initial extraction:", json.dumps(data, ensure_ascii=False, indent=2), "\n")

FOLLOWUP_TABLE = [
    ("age", "كم عمر المريض؟"),
    ("gender", "المريض ذكر ولا أنثى؟"),
    ("duration_mentioned", "من إمتى بدأت الحالة؟"),
    ("cause_mentioned", "شو صار مع المريض؟")
]


for field, question in FOLLOWUP_TABLE:
    if data.get(field) in [None, "", "unknown"]:
        speak(question)
        raw = transcribe(record_audio())
        print(f"Paramedic answered ({field}):", raw, "\n")
        value = interpret_answer(field, question, raw)   # the version that takes the question
        if value != "unknown":
            data[field] = value



# --- Dania's protocol match, before the final print ---
dania_result = run_protocol_flow(data, ask_dania_question)
data = dania_result["data"]

print("\n>> FINAL structured output:")
print(json.dumps(data, ensure_ascii=False, indent=2))
print("\n" + (dania_result.get("match_summary") or "No protocol matched"))

if dania_result.get("suggested_actions"):
    print(dania_result["recommendation"])
    for card in dania_result["suggested_actions"]:
        speak(card["text_ar"])          # one card at a time; long text degrades TTS
else:
    speak("لم يتم تحديد بروتوكول. اتبع التقييم الأولي وتواصل مع الإسعاف المختص.")

# Phase 2: keep asking about vitals until Ctrl+C
run_vitals_monitor(
    data,
    ask=lambda q, f: ask_dania_question(q, f, data),
    speak=speak,
    interval_s=20,   # short for the demo; use 120-180 for real use
)