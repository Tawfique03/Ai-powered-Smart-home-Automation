from vosk import Model
import os, sys

MODEL_PATH = "models/vosk-model-small-en-us-0.15"

if not os.path.isdir(MODEL_PATH):
    print("Model folder not found:", MODEL_PATH)
    sys.exit(1)

try:
    m = Model(MODEL_PATH)
    print("VOSK model loaded OK from", MODEL_PATH)
except Exception as e:
    print("Failed to load model:", e)
