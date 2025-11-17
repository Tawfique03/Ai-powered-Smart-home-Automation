import time, json
from queue import Queue

def reader_thread(q=None, serial_port=None):
    """
    Robust reader thread:
    - If q is None create one (so main.py can call without arguments)
    - If serial_port provided, attempts to open and read lines; otherwise sleeps (dummy mode)
    - For each JSON line read, puts parsed dict into q
    """
    if q is None:
        q = Queue()

    print("[serial_reader] started (queue optional).")
    ser = None
    try:
        if serial_port:
            try:
                import serial
                ser = serial.Serial(serial_port, 9600, timeout=1)
                print(f"[serial_reader] opened serial {serial_port}")
            except Exception as e:
                print("[serial_reader] could not open serial:", e)
                ser = None

        while True:
            if ser:
                try:
                    line = ser.readline().decode('utf-8', errors='ignore').strip()
                except Exception as e:
                    print("[serial_reader] serial read error:", e)
                    line = ''
                if line:
                    try:
                        obj = json.loads(line)
                        q.put(obj)
                    except Exception:
                        q.put({"raw": line})
            else:
                time.sleep(1)
    except KeyboardInterrupt:
        print("[serial_reader] stopped")
    finally:
        if ser and ser.is_open:
            ser.close()
