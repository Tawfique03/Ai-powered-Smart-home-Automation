import threading
import time
import json
from queue import Queue, Empty
from rapidfuzz import fuzz 
import csv
import os
import time

try:
    import serial
    SERIAL_AVAILABLE = True
except Exception:
    SERIAL_AVAILABLE = False
try:
    from flask_app import update_state, emit_voice
except Exception:
    def update_state(s): print("[dashboard:update_state]", s)
    def emit_voice(text, intent=None): print("[dashboard:voice]", intent, text)

# ---------------- Config ----------------
SERIAL_PORT = 'COM5'
BAUD = 9600
SENSOR_LOG_FILE = 'sensor_log.csv'
VOICE_LOG_FILE = 'voice_log.csv'
ACTION_LOG_FILE = 'action_log.csv' 
SENSOR_FIELDNAMES = ['timestamp', 'temp', 'hum', 'pir', 'smoke', 'led', 'fan']
VOICE_FIELDNAMES = ['timestamp', 'text', 'intent']
ACTION_FIELDNAMES = ['timestamp', 'source', 'intent', 'temp', 'hum', 'pir', 'smoke', 'led_state', 'fan_speed']
DEFAULT_OVERRIDE_PRIORITY = {'dashboard': 2, 'voice': 2, 'auto': 1}

# phrases
WAKE_PHRASES = ("hey vista","hey vesta","hi vista","hii vista","hello vista","hello vesta", "hello", "heavy stuff")
SLEEP_PHRASES = ("stop vista","bye vista","sleep","go auto","auto mode","goodbye vista")

WAKEFUZZ_FALLBACK = 75
SLEEPFUZZ_FALLBACK = 75

# ---------------- Controller ----------------
class Controller:
    def __init__(self, serial_port=SERIAL_PORT, baud=BAUD, override_priority=None, ml_brain=None):
        self.serial_port = serial_port
        self.baud = baud
        self.override_priority = override_priority or DEFAULT_OVERRIDE_PRIORITY.copy()
        self.ml_brain = ml_brain 
        self.state = {
            "temp": None, "hum": None, "pir": 0, "smoke": 0,
            "led": False, "fan": 0,
            "led_mode": "auto",
            "fan_mode": "auto"
        }

        self._ser = None
        self._partial_buf = ""
        self._running = False
        self._writer_thread = None
        self._reader_thread = None
        self._sim_thread = None
        self.command_queue = Queue()
        self.voice_active = False
        self._state_lock = threading.RLock()
        
        # --- NEW: CSV Header setup ---
        self._csv_headers = {
            SENSOR_LOG_FILE: SENSOR_FIELDNAMES,
            VOICE_LOG_FILE: VOICE_FIELDNAMES,
            ACTION_LOG_FILE: ACTION_FIELDNAMES,
        }
        self._csv_header_written = {
            SENSOR_LOG_FILE: os.path.exists(SENSOR_LOG_FILE),
            VOICE_LOG_FILE: os.path.exists(VOICE_LOG_FILE),
            ACTION_LOG_FILE: os.path.exists(ACTION_LOG_FILE),
        }
        self._csv_lock = threading.Lock() 


    # --- NEW: GENERIC CSV LOGGING FUNCTION ---
    def _log_to_csv(self, data_dict, filename):
        """Appends a dictionary of data to the specified CSV file."""
        if filename not in self._csv_headers:
            print(f"[controller] Unknown CSV log file: {filename}")
            return

        fieldnames = self._csv_headers[filename]
        data_dict['timestamp'] = time.strftime('%Y-%m-%d %H:%M:%S')

        with self._csv_lock: 
            try:
                with open(filename, 'a', newline='') as f:
                    writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction='ignore')
                    
                    if not self._csv_header_written[filename]:
                        writer.writeheader()
                        self._csv_header_written[filename] = True
                        
                    writer.writerow(data_dict)
                    
            except Exception as e:
                print(f"[controller] Failed to write to {filename}: {e}")


    # ---------------- Serial open/close ----------------
    def _open_serial(self):
        if not SERIAL_AVAILABLE or not self.serial_port:
            return False
        try:
            self._ser = serial.Serial(self.serial_port, self.baud, timeout=1)
            print(f"[controller] opened serial {self.serial_port}@{self.baud}")
            time.sleep(0.1)
            return True
        except Exception as e:
            print(f"[controller] failed to open serial {self.serial_port}: {e}")
            self._ser = None
            return False

    def _close_serial(self):
        try:
            if self._ser and self._ser.is_open:
                self._ser.close()
                print("[controller] serial closed")
        except Exception:
            pass
        self._ser = None

    # ---------------- Low-level send ----------------
    def send_raw(self, s: str):
        """Send raw command to Arduino. If serial absent, apply locally (simulator)."""
        print("→ Arduino:", s)
        if self._ser:
            try:
                if not s.endswith("\n"):
                    s = s + "\n"
                self._ser.write(s.encode('utf-8'))
            except Exception as e:
                print("[controller] serial write failed:", e)
        else:
            self._apply_local_command(s.rstrip("\n"))

    # ---------------- Queue send ----------------
    def send_command(self, cmd: str, source='auto', force=False):
        """ Enqueue a command. """
        self.command_queue.put(cmd)

    # ---------------- Local apply fo immediate UI feedback ----------------
    def _apply_local_command(self, cmd: str):
        """Apply a command to `self.state` when serial absent or for immediate UI feedback."""
        with self._state_lock:
            if cmd.startswith("FAN_PWM:"):
                try:
                    v = int(cmd.split(':',1)[1])
                    self.state['fan'] = max(0, min(255, v))
                    self.state['fan_mode'] = 'manual'
                except Exception:
                    pass
            elif cmd == "FAN_ON":
                self.state['fan'] = 255; self.state['fan_mode'] = 'manual'
            elif cmd == "FAN_OFF":
                self.state['fan'] = 0; self.state['fan_mode'] = 'manual'
            elif cmd == "FAN_AUTO":
                self.state['fan_mode'] = 'auto'
            elif cmd == "LED_ON":
                self.state['led'] = True; self.state['led_mode'] = 'manual'
            elif cmd == "LED_OFF":
                self.state['led'] = False; self.state['led_mode'] = 'manual'
            elif cmd == "LED_AUTO":
                self.state['led_mode'] = 'auto'
            # publish
            update_state(self.state.copy())

    # ---------------- Serial JSON parsing ----------------
    def _process_serial_chunk(self, chunk: str):
        """Buffer arbitrary chunk(s) and extract JSON objects like {"temp":..,"hum":..}"""
        if not chunk:
            return
        self._partial_buf += chunk
        
        while True:
            start = self._partial_buf.find('{')
            end = self._partial_buf.find('}', start if start != -1 else 0)
            if start == -1 or end == -1:
                if len(self._partial_buf) > 4096:
                    last = self._partial_buf.rfind('{')
                    self._partial_buf = self._partial_buf[last:] if last != -1 else ""
                return
            candidate = self._partial_buf[start:end+1]
            self._partial_buf = self._partial_buf[end+1:]
            try:
                obj = json.loads(candidate)
            except Exception:
                print("[controller] failed to parse JSON candidate:", candidate)
                continue
            
            if obj:
                self._log_to_csv(obj, SENSOR_LOG_FILE)

            changed = {}
            with self._state_lock:
                for k in ("temp","hum","pir","smoke","led","fan"):
                    if k in obj:
                        if k in ("led","fan"):
                            mode_key = 'led_mode' if k == 'led' else 'fan_mode'
                            if self.state.get(mode_key, 'auto') == 'auto':
                                self.state[k] = obj[k]
                                changed[k] = obj[k]
                            else:
                                pass 
                        else:
                            self.state[k] = obj[k]
                            changed[k] = obj[k]
                
                self.state.setdefault('led_mode','auto'); self.state.setdefault('fan_mode','auto')
                if changed:
                    publish_state = self.state.copy()
                    update_state(publish_state)

    # ---------------- Intent handling ----------------
    def apply_intent(self, intent: str, text: str=None, source='voice'):
        """
        Handle an intent originating from voice/dashboard/auto.
        This is now the main logging and ML training hub.
        """
        txt = (text or "").lower().strip()
        
        with self._state_lock:
            state_snapshot = self.state.copy()
            current_temp = state_snapshot.get('temp', 20.0)
            current_hum = state_snapshot.get('hum', 50.0)
            current_pir = state_snapshot.get('pir', 0)
            current_smoke = state_snapshot.get('smoke', 0)
            current_led = state_snapshot.get('led', False)

        # --- FUZZY CHECK FUNCTIONS ---
        def text_has_wake_word(text_to_check):
            if not text_to_check: return False
            for w in WAKE_PHRASES:
                if fuzz.partial_ratio(w, text_to_check) >= WAKEFUZZ_FALLBACK:
                    print(f"[controller] Fallback WAKE detected: '{text_to_check}' matches '{w}'")
                    return True
            return False

        def text_has_sleep_word(text_to_check):
            if not text_to_check: return False
            for s in SLEEP_PHRASES:
                if fuzz.partial_ratio(s, text_to_check) >= SLEEPFUZZ_FALLBACK:
                    print(f"[controller] Fallback SLEEP detected: '{text_to_check}' matches '{s}'")
                    return True
            return False


        #Handle WAKE
        is_wake_command = (
            intent == "WAKE" or
            (intent != "VOICE_SLEEP" and text_has_wake_word(txt))
        )
        
        if is_wake_command:
            self.voice_active = True
            emit_voice("Vista: Voice active.", intent="WAKE")
            print("[controller] voice activated")
            # --- NEW: Log voice wake ---
            self._log_to_csv({'text': txt, 'intent': 'WAKE'}, VOICE_LOG_FILE)
            return

        # 2) Handle SLEEP / Revert to Auto
        is_sleep_command = (
            intent == "VOICE_SLEEP" or
            ( (intent == "LOG_SPEECH" or self.voice_active) and text_has_sleep_word(txt) )
        )
        if is_sleep_command:
            
            print("[controller] Sleep command: Forcing LED_AUTO")
            self.send_command("LED_AUTO", source='voice', force=True)
            print("[controller] Sleep command: Forcing FAN_AUTO")
            self.send_command("FAN_AUTO", source='voice', force=True)
            
            with self._state_lock:
                self.state['led_mode'] = 'auto'
                self.state['fan_mode'] = 'auto'
                update_state(self.state.copy())

            self.voice_active = False

            emit_voice("Vista: Voice deactivated. Returning to auto.", intent="SLEEP")
            print("[controller] voice deactivated (sleep phrase)")
            self._log_to_csv({'text': txt, 'intent': 'VOICE_SLEEP'}, VOICE_LOG_FILE)
            return

        # Check Voice Gating
        if source == 'voice' and not self.voice_active:
            print("[controller] voice intent ignored (not active):", intent, txt)
            emit_voice("Vista: Please say the wake word first (e.g. 'hey vista').", intent=None)
            return

        if source == 'voice' and self.voice_active and text:
            if intent != "VOICE_SLEEP" and txt != 'setting auto': 
                emit_voice(f"You: {text}", intent=None)
        
        # Handle non-command speech
        if intent == "LOG_SPEECH":
            print(f"[controller] Heard speech (no command): {text}")
            if self.voice_active:
                emit_voice("Vista: I heard you, but that's not a command I know.", intent="LOG_SPEECH")
            self._log_to_csv({'text': txt, 'intent': 'LOG_SPEECH'}, VOICE_LOG_FILE)
            return

        is_manual_override = (source == 'dashboard' or (source == 'voice' and self.voice_active))
        
        # --- NEW: Prepare data for ACTION_LOG and ML training ---
        action_data = {
            'source': source,
            'intent': intent,
            'temp': current_temp,
            'hum': current_hum,
            'pir': current_pir,
            'smoke': current_smoke,
            'led_state': current_led,
            'fan_speed': self.state.get('fan', 0)
        }
        
        # --- LED ---
        if intent == "LED_ON":
            self.send_command("LED_ON", source=source, force=is_manual_override)
            with self._state_lock:
                self.state['led_mode'] = 'manual'; self.state['led'] = True
                update_state(self.state.copy())
            if source == 'voice': emit_voice("Vista: LED On", intent=intent)
            
            # --- NEW: Log action ---
            action_data['led_state'] = True 
            self._log_to_csv(action_data, ACTION_LOG_FILE)
            return
            
        if intent == "LED_OFF":
            self.send_command("LED_OFF", source=source, force=is_manual_override)
            with self._state_lock:
                self.state['led_mode'] = 'manual'; self.state['led'] = False
                update_state(self.state.copy())
            if source == 'voice': emit_voice("Vista: LED Off", intent=intent)
            
            # --- NEW: Log action ---
            action_data['led_state'] = False
            self._log_to_csv(action_data, ACTION_LOG_FILE)
            return
            
        if intent == "LED_AUTO":
            self.send_command("LED_AUTO", source=source, force=True)
            with self._state_lock:
                self.state['led_mode'] = 'auto'
                update_state(self.state.copy())
            if source == 'voice' and txt != 'setting auto':
                emit_voice("Vista: LED set to Auto", intent=intent)
            
            self._log_to_csv(action_data, ACTION_LOG_FILE)
            return

        # --- FAN ---
        new_fan_speed = None 
        
        if intent == "FAN_ON":
            new_fan_speed = 255
            self.send_command("FAN_ON", source=source, force=is_manual_override)
            with self._state_lock:
                self.state['fan_mode'] = 'manual'; self.state['fan'] = new_fan_speed
                update_state(self.state.copy())
            if source == 'voice': emit_voice("Vista: Fan On", intent=intent)
            
        elif intent == "FAN_OFF":
            new_fan_speed = 0
            self.send_command("FAN_OFF", source=source, force=is_manual_override)
            with self._state_lock:
                self.state['fan_mode'] = 'manual'; self.state['fan'] = new_fan_speed
                update_state(self.state.copy())
            if source == 'voice': emit_voice("Vista: Fan Off", intent=intent)
            
        elif intent == "FAN_AUTO":
            self.send_command("FAN_AUTO", source=source, force=True)
            with self._state_lock:
                self.state['fan_mode'] = 'auto'
                update_state(self.state.copy())
            if source == 'voice' and txt != 'setting auto':
                emit_voice("Vista: Fan set to Auto", intent=intent)
            self._log_to_csv(action_data, ACTION_LOG_FILE)
            return
            
        elif intent.startswith("FAN_PWM"):
            self.send_command(intent, source=source, force=is_manual_override)
            with self._state_lock:
                self.state['fan_mode'] = 'manual'
                try:
                    v = int(intent.split(':',1)[1]); 
                    new_fan_speed = max(0,min(255,v))
                    self.state['fan'] = new_fan_speed
                except Exception: 
                    new_fan_speed = self.state.get('fan', 0) 
                update_state(self.state.copy())
            if source == 'voice': emit_voice(f"Vista: Fan set to {new_fan_speed}", intent=intent)

        elif intent.startswith("QUICK:"):
            mode_name = "custom"
            try:
                m = intent.split(':',1)[1]
                if m == 'comfort': new_fan_speed = 170; mode_name="Comfort"
                elif m == 'eco': new_fan_speed = 70; mode_name="Eco"
                elif m == 'boost': new_fan_speed = 255; mode_name="Boost"
            except Exception as e:
                print(f"[controller] Invalid quick mode: {intent}, error: {e}")
                new_fan_speed = self.state.get('fan', 0)
            
            self.send_command(f"FAN_PWM:{new_fan_speed}", source=source, force=is_manual_override)
            with self._state_lock:
                self.state['fan_mode'] = 'manual'
                self.state['fan'] = new_fan_speed
                update_state(self.state.copy())
            if source == 'voice': emit_voice(f"Vista: Setting fan to {mode_name} mode.", intent=intent)
        
        
        if new_fan_speed is not None: 
            action_data['fan_speed'] = new_fan_speed
            self._log_to_csv(action_data, ACTION_LOG_FILE)
            
            if self.ml_brain and current_temp is not None and current_hum is not None:
                print(f"[controller] Training ML Regressor: (T:{current_temp}, H:{current_hum}, L:{current_led}, P:{current_pir}) -> {new_fan_speed}")
                try:
                    self.ml_brain.update_regressor(
                        temp=current_temp,
                        hum=current_hum,
                        led_state=current_led,
                        pir=current_pir,
                        fan_label=new_fan_speed
                    )
                except Exception as e:
                    print(f"[controller] ML Regressor training failed: {e}")
            
        if source == 'voice' and intent not in ['WAKE', 'VOICE_SLEEP', 'LOG_SPEECH', 'LED_AUTO', 'FAN_AUTO']:
            self._log_to_csv({'text': txt, 'intent': intent}, VOICE_LOG_FILE)
            
            if self.ml_brain and text: 
                print(f"[controller] Training ML Classifier: ('{text}') -> {intent}")
                try:
                    self.ml_brain.update_intent(text=text, label=intent)
                except Exception as e:
                    print(f"[controller] ML Classifier training failed: {e}")
        
        if new_fan_speed is not None:
            return 

        if source == 'dashboard':
            self.send_command(intent, source='dashboard', force=True)
            print("[controller] dashboard sent fallback command:", intent)
            return

        print("[controller] unknown intent:", intent, "text:", text)
        if source == 'voice' and self.voice_active:
            emit_voice(f"Vista: Sorry, I don't understand '{text}'", intent=None)


    def _writer_loop(self):
        while self._running:
            try:
                cmd = self.command_queue.get(timeout=0.2)
            except Empty:
                continue
            try:
                self.send_raw(cmd)
            except Exception as e:
                print("[controller] failed to send command:", e)

    def _reader_loop(self):
        if not self._ser:
            return
        while self._running:
            try:
                raw = self._ser.readline()
                try:
                    chunk = raw.decode('utf-8', errors='ignore')
                except Exception:
                    chunk = raw.decode('latin-1', errors='ignore')
            except Exception as e:
                print("[controller] serial read error:", e)
                chunk = ''
            if chunk:
                self._process_serial_chunk(chunk)

    def _simulator_loop(self):
        t = 22.0; h = 45.0; step = 0
        while self._running:
            t += (0.1 if step % 10 < 5 else -0.1)
            h += (0.2 if step % 15 < 8 else -0.2)
            discomfort = t + 0.1*h
            fan_pwm = int(min(255, max(0, (discomfort - 28) / (40 - 28) * 255))) if discomfort > 28 else 0
            
            sim_pir = 0
            with self._state_lock:
                
                if self.state.get('fan_mode') == 'auto':
                    if self.state.get('led', False):
                        self.state['fan'] = fan_pwm
                    else:
                        self.state['fan'] = 0
                
                if self.state.get('led_mode') == 'auto':
                    if step % 20 == 0: 
                        sim_pir = 1
                        self.state['led'] = not self.state['led'] 
                    else:
                        sim_pir = 0
                self.state['pir'] = sim_pir
                self.state['temp'] = round(t,1)
                self.state['hum'] = round(h,0)
                
                sim_data = {
                    "temp": self.state['temp'],
                    "hum": self.state['hum'],
                    "pir": self.state['pir'],
                    "smoke": self.state.get('smoke', 0),
                    "led": self.state.get('led', False),
                    "fan": self.state.get('fan', 0)
                }
                self._log_to_csv(sim_data, SENSOR_LOG_FILE)
            
            update_state(self.state.copy())
            step += 1
      
            time.sleep(1.0)

    # ---------------- Start / Stop ----------------
    def start(self):
        if self._running:
            return
        self._running = True

        has_serial = False
        if self.serial_port and SERIAL_AVAILABLE:
            has_serial = self._open_serial()

        self._writer_thread = threading.Thread(target=self._writer_loop, daemon=True)
        self._writer_thread.start()

        if has_serial:
            self._reader_thread = threading.Thread(target=self._reader_loop, daemon=True)
            self._reader_thread.start()
        else:
            self._sim_thread = threading.Thread(target=self.d_simulator_loop, daemon=True)
            self._sim_thread.start()

        print("[controller] started")

    def stop(self):
        self._running = False
        try:
            self._close_serial()
        except Exception:
            pass
        print("[controller] stopped")

# ---------------- CLI/Test ----------------
if __name__ == '__main__':
    
    # Create a dummy ML brain
    class DummyML:
        def update_regressor(self, **kwargs):
            print(f"[DummyML] UPDATE REGRESSOR with {kwargs}")
        def update_intent(self, **kwargs):
            print(f"[DummyML] UPDATE INTENT with {kwargs}")
        def start(self):
            print("[DummyML] start")
        def stop(self):
            print("[DummyML] stop")
            
    ml_test_brain = DummyML()
    
    # Start controller in sim mode, linked to dummy brain
    c = Controller(serial_port=None, ml_brain=ml_test_brain)
    c.start()
    print("Controller (simulator) running with DummyML.")
    
    try:
        # Simulate some actions
        time.sleep(2)
        print("\n--- SIMULATING 'LED_ON' from dashboard ---")
        c.apply_intent("LED_ON", text="LED_ON", source="dashboard")
        time.sleep(2)
        print("\n--- SIMULATING 'FAN_PWM:150' from dashboard ---")
        c.apply_intent("FAN_PWM:150", text="FAN_PWM:150", source="dashboard")
        time.sleep(2)
        print("\n--- SIMULATING 'WAKE' from voice ---")
        c.apply_intent("WAKE", text="hey vista", source="voice")
        time.sleep(1)
        print("\n--- SIMULATING 'FAN_OFF' from voice ---")
        c.apply_intent("FAN_OFF", text="fan off", source="voice")
        time.sleep(5)
        
        print("\n--- SIMULATING 'VOICE_SLEEP' from voice ---")
        c.apply_intent("VOICE_SLEEP", text="go auto", source="voice")
        
        while True:
            time.sleep(1)
            
    except KeyboardInterrupt:
        c.stop()