from groq import Groq

client = Groq(api_key="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")

# paste in the transcription from your STT script
transcribed_text = "مريض مات، مات، وعندو صاقة كهربائية بقلبو."

prompt = f"""You are a medical NLU extraction system for a paramedic voice assistant.
Given the following transcribed Arabic speech from a paramedic, extract structured information.

Transcribed text: "{transcribed_text}"

Return ONLY valid JSON with this exact structure, no other text:
{{
  "patient_status": "<alive/deceased/unknown>",
  "symptoms": ["<list of symptom keywords in English>"],
  "cause_mentioned": "<cause if mentioned, else null>",
  "duration_mentioned": "<duration if mentioned, else null>",
  "confidence": "<high/medium/low>"
}}"""

response = client.chat.completions.create(
    model="openai/gpt-oss-120b",
    max_tokens=300,
    messages=[{"role": "user", "content": prompt}]
)

print(response.choices[0].message.content)