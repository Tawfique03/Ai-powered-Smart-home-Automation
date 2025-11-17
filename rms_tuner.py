import numpy as np, sounddevice as sd, time

RATE = 16000
FRAME_MS = 30
FRAME_SAMPLES = int(RATE * FRAME_MS / 1000)

def rms(frame):
    a = np.frombuffer(frame, dtype=np.int16).astype(float)
    return (a*a).mean()**0.5

stream = sd.RawInputStream(samplerate=RATE, blocksize=FRAME_SAMPLES,
                           dtype='int16', channels=1)
stream.start()
print("Recording... press Ctrl+C to stop. Speak and stay silent to measure levels.")
try:
    while True:
        data, _ = stream.read(FRAME_SAMPLES)
        if len(data)==0: continue
        print(int(rms(data)))
        time.sleep(0.01)
except KeyboardInterrupt:
    print("Done.")
finally:
    stream.stop(); stream.close()
