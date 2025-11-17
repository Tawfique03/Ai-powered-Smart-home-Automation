from voice_handler import VoiceHandler

def on_intent(intent, text, source='voice'):
    print(f"INTENT RECEIVED -> {intent} | text: {text} | source: {source}")

vh = VoiceHandler(
    model_path="models/vosk-model-small-en-us-0.15",
    on_intent_callback=on_intent
)

vh.start()

print("Running — say 'hey vesta, turn light on' or 'turn fan off'")
try:
    while True:
        pass
except KeyboardInterrupt:
    vh.stop()
    print("Stopped.")
