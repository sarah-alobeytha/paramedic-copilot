import sounddevice as sd
import soundfile as sf
import numpy as np
from transformers import pipeline, VitsModel, AutoTokenizer
from groq import Groq
import json
import torch

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
["chest_pain", "shortness_of_breath", "fever", "bleeding", "fracture", "burn",
"electric_shock", "seizure", "unconscious", "vomiting", "abdominal_pain",
"head_injury", "cardiac_arrest", "stroke_symptoms", "allergic_reaction"]

If none fit, use "other" and describe it in cause_mentioned.

Return ONLY valid JSON:
{{
  "patient_status": "<alive/deceased/unknown>",
  "symptoms": ["<from the list above>"],
  "cause_mentioned": "<cause if mentioned, else null>",
  "duration_mentioned": "<duration if mentioned, else null>",
  "confidence": "<high/medium/low>"
}}"""
    response = groq_client.chat.completions.create(
        model="openai/gpt-oss-120b",
        max_tokens=500,
        messages=[{"role": "user", "content": prompt}]
    )
    return json.loads(response.choices[0].message.content)

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
        if value not in [None, "", [], "unknown"]:
            base[key] = value
    return base

# ---- Main flow ----
audio_file = record_audio()
text = transcribe(audio_file)
print("Transcribed text:", text, "\n")

data = extract_structured(text)
print("Initial extraction:", json.dumps(data, ensure_ascii=False, indent=2), "\n")

CRITICAL_FIELDS = ["duration_mentioned", "cause_mentioned"]
max_followups = 2
followups_asked = 0

for field in CRITICAL_FIELDS:
    if followups_asked >= max_followups:
        break
    if data.get(field) is None:
        question = generate_followup_question(field, data)
        print(f"\nAsking: {question}\n")
        speak(question)  # <-- now actually spoken aloud
        audio_file = record_audio()
        answer_text = transcribe(audio_file)
        print("Paramedic answered:", answer_text, "\n")
        new_data = extract_structured(answer_text)
        data = merge_data(data, new_data)
        followups_asked += 1

print("\n>> FINAL structured output:")
print(json.dumps(data, ensure_ascii=False, indent=2))
