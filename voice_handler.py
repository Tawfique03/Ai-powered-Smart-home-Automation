import sounddevice as sd
import numpy as np
import json
import time
import threading
import queue
import pyttsx3
import re
import difflib
from vosk import Model, KaldiRecognizer
from rapidfuzz import fuzz

DEBUG_RMS = False
DEBUG_PARTIAL = True
DEBUG_EVENTS = True

SAMPLE_RATE = 16000
FRAME_MS = 30
FRAME_SAMPLES = int(SAMPLE_RATE * FRAME_MS / 1000)

SILENCE_DURATION = 0.35
SILENCE_FRAMES = int(SILENCE_DURATION / (FRAME_MS/1000))
CALIBRATE_SECONDS = 1.5

# Wake words and thresholds (include single-word wakes)
WAKE_WORDS = ["hello", "hey", "hey vesta", "vesta", "hey vista", "hi vista", "hey there"]
WAKEFUZZ = 65  # lowered to be a bit more permissive on common mics

# Intent map (phrase -> canonical)
INTENT_MAP = {
    "turn light on": "LED_ON",
    "turn light off": "LED_OFF",
    "light on": "LED_ON",
    "light off": "LED_OFF",
    "turn fan on": "FAN_ON",
    "turn fan off": "FAN_OFF",
    "fan on": "FAN_ON",
    "fan off": "FAN_OFF",
    "set fan auto": "FAN_AUTO",
    "auto fan": "FAN_AUTO",
    "fan auto": "FAN_AUTO",
    "set led auto": "LED_AUTO",
    "auto led": "LED_AUTO",
    "led auto": "LED_AUTO",
    "auto mode": "VOICE_SLEEP",
    "go auto": "VOICE_SLEEP",
    "status": "STATUS",
    "stop vista": "VOICE_SLEEP",
    "bye vista": "VOICE_SLEEP",
    "stop vesta": "VOICE_SLEEP",
    "bye vesta": "VOICE_SLEEP"
}

INTENT_FUZZ = 70      
KEYWORD_FUZZ = 58     
ON_OFF_FUZZ = 70
INTENT_COOLDOWN = 0.6
# Casual speech filter 
MISHEAR_MAP = {
    "right": "light",
    "riht": "light",
    "lite": "light",
    "than": "fan",
    "then": "fan",
    "pan": "fan",
    "man": "fan",
    "van": "fan",
    "of": "off",
    "heavy stuff": "hey vesta"
    
}


# ---------------- Non-blocking TTS ----------------
class NonBlockingTTS:
    def __init__(self, rate=150):
        self.q = queue.Queue()
        self.engine = None
        try:
            self.engine = pyttsx3.init()
            self.engine.setProperty('rate', rate)
            self.t = threading.Thread(target=self._worker, daemon=True)
            self.t.start()
        except Exception as e:
            print("[TTS] init error:", e)
            self.engine = None

    def _worker(self):
        while True:
            txt = self.q.get()
            try:
                if self.engine:
                    self.engine.say(txt)
                    self.engine.runAndWait()
            except Exception as e:
                print("[TTS] speak error:", e)

    def speak(self, text):
        if self.engine:
            self.q.put(text)

# ---------------- Voice Handler ----------------
class VoiceHandler:
    def __init__(self, model_path, on_intent_callback, tts_enabled=True, on_rms_callback=None):
        """
        model_path: path to extracted VOSK model directory
        on_intent_callback: function(intent_label:str, text:str, source='voice')
            - special intent 'WAKE' used for wake-only events
            - special intent 'VOICE_SLEEP' used to go back to auto
        on_rms_callback: function(level:int) - for dashboard UI
        """
        self.on_intent = on_intent_callback
        try:
            self.model = Model(model_path)
        except Exception as e:
            print("[VoiceHandler] Failed to load VOSK model:", e)
            print("[VoiceHandler] Make sure your VOSK_MODEL path in main.py is correct!")
            raise
        self.rec = KaldiRecognizer(self.model, SAMPLE_RATE)
        self._stop = threading.Event()
        self.tts = NonBlockingTTS() if tts_enabled else None
        self._listen_thread = None
        self._last_intent_time = 0.0
        self.threshold = None  
        
        self.on_rms = on_rms_callback
        self._last_rms_time = 0.0

    def _rms(self, frame):
        a = np.frombuffer(frame, dtype=np.int16).astype(np.float32)
        return np.sqrt(np.mean(a*a)) if a.size else 0.0

    def _map_intent(self, text):
        t = text.lower().strip()
        if not t:
            return None

        # 1. exact or fuzzy phrase match
        if t in INTENT_MAP:
            return INTENT_MAP[t]
        best = (None, 0)
        for k, v in INTENT_MAP.items():
            s = fuzz.ratio(k, t)
            if s > best[1]:
                best = (v, s)
        if best[1] >= INTENT_FUZZ:
            return best[0]

        # 2. Keyword matching
        words = re.findall(r"[a-zA-Z]+", t)
        if not words:
            return None

        device = None
        action = None
        
        # *** FIX: Simplified device detection ***
        # First, check the word itself
        if "light" in words: device = "light"
        elif "fan" in words: device = "fan"
        else:
            # If not found, check the mishear map
            for w in words:
                if w in MISHEAR_MAP:
                    device = MISHEAR_MAP[w] # device becomes "light" or "fan"
                    break

     
        # detect on/off
        for w in words:
            if fuzz.partial_ratio("on", w) >= ON_OFF_FUZZ:
                action = "on"; break
            if fuzz.partial_ratio("off", w) >= ON_OFF_FUZZ:
                action = "off"; break
        if action is None:
            if re.search(r'\bon\b', t): action = "on"
            if re.search(r'\boff\b', t): action = "off"
        

        if device and action:
            if device == "fan":
                return "FAN_ON" if action == "on" else "FAN_OFF"
            else: 
                return "LED_ON" if action == "on" else "LED_OFF"

        # loose fallback for very short inputs
        if len(t.split()) <= 2:
            for k, v in INTENT_MAP.items():
                s = fuzz.partial_ratio(k, t)
                if s >= (INTENT_FUZZ - 8):
                    return v

        return None

    def _calibrate_threshold(self, stream, seconds=CALIBRATE_SECONDS):
        if DEBUG_EVENTS:
            print("[VoiceHandler] Calibrating ambient noise for", seconds, "seconds...")
        samples = []
        frames = int((seconds * 1000) / FRAME_MS)
        for _ in range(frames):
            data, _ = stream.read(FRAME_SAMPLES)
            if len(data) == 0:
                continue
            samples.append(self._rms(data))
        if not samples:
            self.threshold = 700
        else:
            mean_silence = float(np.mean(samples))
            self.threshold = max(150.0, mean_silence * 3.0)
        if DEBUG_EVENTS:
            print("[VoiceHandler] Calibrated threshold:", int(self.threshold))

    def start(self):
        self._stop.clear()
        self._listen_thread = threading.Thread(target=self._audio_loop, daemon=True)
        self._listen_thread.start()
        print("VoiceHandler started (auto-calibrate).")

    def stop(self):
        self._stop.set()
        if self._listen_thread:
            self._listen_thread.join(timeout=1)

    def _audio_loop(self):
        try:
            stream = sd.RawInputStream(samplerate=SAMPLE_RATE, blocksize=FRAME_SAMPLES,
                                       dtype='int16', channels=1)
            stream.start()
        except Exception as e:
            print("[VoiceHandler] Failed to open audio stream:", e)
            print("[VoiceHandler] This may be a microphone permissions or selection issue.")
            return

        try:
            self._calibrate_threshold(stream)
        except Exception as e:
            print("[VoiceHandler] Calibration failed, using default threshold. Err:", e)
            self.threshold = 700

        buf = bytearray()
        started = False
        silence_counter = 0

        try:
            while not self._stop.is_set():
                try:
                    data, _ = stream.read(FRAME_SAMPLES)
                except Exception as e:
                    if DEBUG_EVENTS:
                        print("[VoiceHandler] Audio read error:", e)
                    time.sleep(0.05)
                    continue

                if len(data) == 0:
                    time.sleep(0.001)
                    continue

                level = self._rms(data)

                # Send RMS level to dashboard (throttled)
                now = time.time()
                if self.on_rms and (now - self._last_rms_time > 0.2): 
                    try:
                        self.on_rms(int(level))
                        self._last_rms_time = now
                    except Exception:
                        pass 
                
                if DEBUG_RMS:
                    print("[VoiceHandler] RMS", int(level))

                if level > self.threshold:
                    buf.extend(data)
                    started = True
                    silence_counter = 0
                    try:
                        self.rec.AcceptWaveform(data)
                    except Exception:
                        pass
                else:
                    if started:
                        buf.extend(data)
                        silence_counter += 1
                        if DEBUG_PARTIAL:
                            try:
                                pr = json.loads(self.rec.PartialResult())
                                if 'partial' in pr and pr['partial']:
                                    print("[VoiceHandler] PARTIAL:", pr['partial'])
                            except Exception:
                                pass

                        if silence_counter > SILENCE_FRAMES:
                            try:
                                if self.rec.AcceptWaveform(bytes(buf)):
                                    res = json.loads(self.rec.Result())
                                else:
                                    res = json.loads(self.rec.FinalResult())
                            except Exception as e:
                                if DEBUG_EVENTS:
                                    print("[VoiceHandler] VOSK recognition error:", e)
                                res = {}

                            text = res.get('text', '').strip()
                            if text:
                                if DEBUG_EVENTS:
                                    print("[VoiceHandler] HEARD:", text)

                                original_text = text  

                                lw_ok = False
                                matched_wake = None
                                for w in WAKE_WORDS:
                                    if fuzz.partial_ratio(w, original_text) >= WAKEFUZZ:
                                        lw_ok = True
                                        matched_wake = w
                                        break

                                
                                if lw_ok:
                                    
                                    pattern = re.compile(re.escape(matched_wake), re.IGNORECASE)
                                    tail = pattern.sub('', original_text, count=1).strip()
                                    if tail == '' or len(tail.split()) < 2:
                                        if DEBUG_EVENTS:
                                            print("[VoiceHandler] WAKE detected (wake-only):", original_text)
                                        try:
                                            self.on_intent("WAKE", original_text, source='voice')
                                        except Exception as e:
                                            print("[VoiceHandler] on_intent callback error (WAKE):", e)
                                        if self.tts:
                                            self.tts.speak("Yes?")
                                        # reset buffers and recognizer
                                        buf = bytearray()
                                        started = False
                                        silence_counter = 0
                                        self.rec = KaldiRecognizer(self.model, SAMPLE_RATE)
                                        # apply cooldown so WAKE isn't repeated
                                        self._last_intent_time = time.time()
                                        continue
                                    else:
                                        text_for_intent = tail
                                else:
                                    text_for_intent = original_text

                                now = time.time()
                                if now - self._last_intent_time < INTENT_COOLDOWN:
                                    if DEBUG_EVENTS:
                                        print("[VoiceHandler] In cooldown, ignoring:", text_for_intent)
                                else:
                                    intent = self._map_intent(text_for_intent)
                                    if intent:
                                        try:
                                            self.on_intent(intent, text_for_intent, source='voice')
                                        except Exception as e:
                                            print("[VoiceHandler] on_intent callback error:", e)
                                        if self.tts:
                                            friendly = {
                                                "LED_ON": "light on",
                                                "LED_OFF": "light off",
                                                "FAN_ON": "fan on",
                                                "FAN_OFF": "fan off",
                                                "LED_AUTO": "LED auto", 
                                                "FAN_AUTO": "fan auto",
                                                "VOICE_SLEEP": "going to auto"
                                            }
                                            speak_txt = friendly.get(intent, text_for_intent)
                                            self.tts.speak(f"Okay, {speak_txt}")
                                        self._last_intent_time = now
                                    else:
                                        if DEBUG_EVENTS:
                                            print("[VoiceHandler] No intent matched for:", text_for_intent)

                                        try:
                                            self.on_intent("LOG_SPEECH", text_for_intent, source='voice')
                                        except Exception as e:
                                            print("[VoiceHandler] on_intent callback error (LOG_SPEECH):", e)

                            buf = bytearray()
                            started = False
                            silence_counter = 0
                            self.rec = KaldiRecognizer(self.model, SAMPLE_RATE)
                    else:
                        pass

                time.sleep(0.001)
        finally:
            try:
                stream.stop()
                stream.close()
            except Exception:
                pass
