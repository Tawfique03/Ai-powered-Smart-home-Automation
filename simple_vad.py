import numpy as np
import sounddevice as sd
from vosk import Model, KaldiRecognizer
import json, time

SAMPLE_RATE = 16000
FRAME_MS = 30
FRAME_SAMPLES = int(SAMPLE_RATE * FRAME_MS / 1000)
THRESHOLD = 700   
SILENCE_DURATION = 0.6  
SILENCE_FRAMES = int(SILENCE_DURATION / (FRAME_MS/1000))

MODEL_PATH = "models/vosk-model-small-en-us-0.15" 

def rms_int16(frame):
    a = np.frombuffer(frame, dtype=np.int16).astype(np.float32)
    return np.sqrt(np.mean(a*a))

def handle_utterance(wav_bytes, rec):
    if rec.AcceptWaveform(wav_bytes):
        res = json.loads(rec.Result())
        text = res.get("text", "")
        if text:
            print("Recognized:", text)
    else:
        res = json.loads(rec.FinalResult())
        print("Final:", res.get("text", ""))

def listen():
    print("Loading VOSK model (this may take a moment)...")
    model = Model(MODEL_PATH)
    rec = KaldiRecognizer(model, SAMPLE_RATE)
    stream = sd.RawInputStream(samplerate=SAMPLE_RATE, blocksize=FRAME_SAMPLES,
                               dtype='int16', channels=1)
    stream.start()
    print("Listening (energy VAD)...")
    started = False
    silence_counter = 0
    buffer_bytes = bytearray()
    try:
        while True:
            data, _ = stream.read(FRAME_SAMPLES)
            if len(data) == 0:
                continue
            level = rms_int16(data)
            if level > THRESHOLD:
                buffer_bytes.extend(data)
                started = True
                silence_counter = 0
            else:
                if started:
                    buffer_bytes.extend(data)
                    silence_counter += 1
                    if silence_counter > SILENCE_FRAMES:
                        handle_utterance(bytes(buffer_bytes), rec)
                        buffer_bytes = bytearray()
                        started = False
                        silence_counter = 0
                else:
                    pass
    except KeyboardInterrupt:
        print("Stopped.")
    finally:
        stream.stop()
        stream.close()

if __name__ == '__main__':
    listen()
