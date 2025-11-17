import importlib
import threading
import time
import sys
import traceback

# CONFIG
VOSK_MODEL = "models/vosk-model-small-en-us-0.15"  
HOST = "0.0.0.0"
PORT = 5000

def start_thread_from_module(mod, candidates=("start","run","main")):
    """
    If module has a callable `start/run/main`, call it in a daemon thread.
    Return thread or None.
    """
    for name in candidates:
        fn = getattr(mod, name, None)
        if callable(fn):
            t = threading.Thread(target=fn, daemon=True)
            t.start()
            print(f"[main] Started {mod.__name__}.{name}() in thread.")
            return t
    for name in ("serial_reader_thread","reader_thread","worker","serve"):
        fn = getattr(mod, name, None)
        if callable(fn):
            t = threading.Thread(target=fn, daemon=True)
            t.start()
            print(f"[main] Started {mod.__name__}.{name}() in thread.")
            return t
    print(f"[main] Module {mod.__name__} has no start/run/main - no thread started.")
    return None

def safe_import(name):
    try:
        m = importlib.import_module(name)
        print(f"[main] Imported module: {name}")
        return m
    except Exception as e:
        print(f"[main] Failed to import {name}: {e}")
        return None

ml_mod = safe_import("ml_brain")
ml_brain_instance = None
if ml_mod:
    ml_brain_instance = getattr(ml_mod, "default_ml", None)
    if ml_brain_instance:
        print("[main] ML Brain instance loaded.")
    else:
        print("[main] Could not load default_ml instance from ml_brain.")
else:
    print("[main] ml_brain not present or failed to import (ok).")

controller_mod = safe_import("controller")
if controller_mod is None:
    print("[main] controller.py missing — aborting.")
    sys.exit(1)

Controller = getattr(controller_mod, "Controller", None)
if Controller is None:
    print("[main] No Controller class found in controller.py — expecting functions send_command()/apply_intent().")
    controller = controller_mod  
else:
    try:
        sp = getattr(controller_mod, "SERIAL_PORT", None)
        
        controller = Controller(
            serial_port=sp if sp is not None else None,
            ml_brain=ml_brain_instance
        )
        print("[main] Controller instantiated and connected to ML Brain.")
    except Exception as e:
        print("[main] Controller instantiation failed:", e)
        traceback.print_exc()
        sys.exit(1)

serial_mod = safe_import("serial_reader")
if serial_mod:
    serial_thread = start_thread_from_module(serial_mod)
else:
    print("[main] serial_reader not present — controller may simulate sensors.")

if ml_brain_instance:
    if hasattr(ml_brain_instance, "start") and callable(ml_brain_instance.start):
        ml_brain_instance.start()
        print("[main] ML Brain background trainer started.")
    else:
        print("[main] ML Brain instance has no start() method.")


vh_mod = safe_import("voice_handler")
voice_handler_instance = None
if vh_mod:
    VH = getattr(vh_mod, "VoiceHandler", None)
    if VH is None:
        print("[main] voice_handler.py has no VoiceHandler class. If it exposes a start() function, we will call it instead.")
        voice_thread = start_thread_from_module(vh_mod)
    else:

        def on_intent(intent, text, source='voice'):
            print(f"[main] on_intent received: {intent} | text: {text} | source: {source}")
            if hasattr(controller, "apply_intent"):
                try:
                    controller.apply_intent(intent, text, source=source)
                    return
                except Exception as e:
                    print("[main] controller.apply_intent failed:", e)

            if hasattr(controller, "send_command"):
                try:
                    controller.send_command(intent, source=source)
                except Exception as e:
                    print("[main] controller.send_command failed:", e)
            else:
                print("[main] No controller method to accept intent. Intent dropped.")

        try:
            try:
                voice_handler_instance = VH(
                    model_path=VOSK_MODEL,
                    on_intent_callback=on_intent,
                    tts_enabled=True,
                    on_rms_callback=None 
                )
            except TypeError as e:
                print(f"[main] VoiceHandler init failed: {e}")
                voice_handler_instance = VH(model_path=VOSK_MODEL, on_intent_callback=on_intent, tts_enabled=True)

            if hasattr(voice_handler_instance, "start") and callable(voice_handler_instance.start):
                voice_handler_instance.start()
                print("[main] VoiceHandler.start() called.")
            else:
                if hasattr(voice_handler_instance, "run") and callable(voice_handler_instance.run):
                    t = threading.Thread(target=voice_handler_instance.run, daemon=True)
                    t.start()
                    print("[main] VoiceHandler.run() started in thread.")
                else:
                    print("[main] VoiceHandler instance has no start/run method.")
        except Exception as e:
            print("[main] Failed to create/start VoiceHandler:", e)
            traceback.print_exc()
else:
    print("[main] voice_handler not present — voice disabled.")

flask_mod = safe_import("flask_app")
if flask_mod is None:
    print("[main] flask_app.py missing — cannot start dashboard. Exiting.")
    sys.exit(1)

try:
    set_cb = getattr(flask_mod, "set_controller_callback", None)
    if set_cb and callable(set_cb) and hasattr(controller, "apply_intent"):
        set_cb(controller.apply_intent)
        print("[main] Plumbed flask_app commands to controller.apply_intent")
    else:
        print("[main] WARNING: Could not plumb flask_app to controller. Dashboard buttons may not work.")
except Exception as e:
    print("[main] Failed to plumb flask_app:", e)

try:
    publish_rms_cb = getattr(flask_mod, "publish_rms", None)
    if publish_rms_cb and voice_handler_instance:
        voice_handler_instance.on_rms = publish_rms_cb
        print("[main] Plumbed voice RMS to flask_app")
except Exception as e:
    print("[main] Failed to plumb RMS:", e)


if hasattr(controller, "start") and callable(controller.start):
    try:
        controller.start()
        print("[main] controller.start() called.")
    except Exception as e:
        print("[main] controller.start() raised:", e)
elif hasattr(controller, "run") and callable(controller.run):
    t = threading.Thread(target=controller.run, daemon=True)
    t.start()
    print("[main] Started controller.run() in thread.")
else:
    print("[main] Controller has no start/run method.")

try:
    socketio = getattr(flask_mod, "socketio", None)
    app = getattr(flask_mod, "app", None)

    if app is None:
        print("[main] flask_app.py missing `app`. Cannot start dashboard.")
        sys.exit(1)

    if socketio is None:
        print("[main] No socketio found — running Flask app directly (SSE mode).")
        print(f"[main] Starting dashboard at http://{HOST}:{PORT}")
        app.run(host=HOST, port=PORT, threaded=True)
        sys.exit(0)

    print(f"[main] Starting dashboard at http://{HOST}:{PORT}")
    socketio.run(app, host=HOST, port=PORT, allow_unsafe_werkzeug=True)

except KeyboardInterrupt:
    print("[main] KeyboardInterrupt — shutting down.")

except Exception as e:
    print("[main] Failed to start dashboard:", e)
    traceback.print_exc()

finally:
    try:
        if hasattr(controller, "stop"):
            controller.stop()
        if voice_handler_instance and hasattr(voice_handler_instance, "stop"):
            voice_handler_instance.stop()
        if ml_brain_instance and hasattr(ml_brain_instance, "stop"):
            ml_brain_instance.stop() # Save models on exit
            print("[main] ML Brain stopped and models saved.")
    except Exception:
        pass