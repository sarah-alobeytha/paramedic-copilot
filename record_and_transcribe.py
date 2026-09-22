import sounddevice as sd
import soundfile as sf
import numpy as np
from transformers import pipeline

print("Loading model...")
pipe = pipeline("automatic-speech-recognition", model="oddadmix/whisper-large-v3-turbo-arabic-dialectal-v2")
print("Model ready.\n")

SAMPLE_RATE = 16000

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
print(f"Saved {len(recording)/SAMPLE_RATE:.1f} seconds. Transcribing...")

result = pipe("recording.wav")
print("\n>>", result["text"])