from threading import Thread, Event, Lock
from queue import Queue, Empty
import time, os
import numpy as np
import joblib
from sklearn.linear_model import SGDRegressor, SGDClassifier
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.preprocessing import StandardScaler

MODEL_FNAME = "ml_models.pkl"

class MLBrain:
    def __init__(self, model_path=MODEL_FNAME, verbose=False):
        self.model_path = model_path
        self.verbose = verbose

        self.reg = None
        self.vec = None
        self.clf = None
        self.scaler = None  

        self._train_q = Queue()
        self._stop_evt = Event()
        self._trainer = None
        self._lock = Lock()

        self._bootstrap()

    def _log(self, *a, **k):
        if self.verbose:
            print("[MLBrain]", *a, **k)

    def _bootstrap(self):
        """Create initial models so partial_fit works and scaler is initialized."""
        X = np.array([[25.0, 40.0, 1, 1],
                      [22.0, 45.0, 1, 0],
                      [30.0, 60.0, 1, 1],
                      [20.0, 30.0, 0, 0]])
        y = np.array([0.0, 0.0, 200.0, 0.0])

        self.scaler = StandardScaler()
        try:
            self.scaler.partial_fit(X)
        except Exception:
            try:
                self.scaler.fit(X)
            except Exception:
                pass

        try:
            self.reg = SGDRegressor(max_iter=1000, tol=1e-3)
            Xs = self.scaler.transform(X)
            self.reg.fit(Xs, y)
        except Exception as e:
            self._log("regressor bootstrap fit failed:", e)
            self.reg = SGDRegressor(max_iter=1000, tol=1e-3)
            try:
                self.reg.partial_fit(self.scaler.transform(X), y)
            except Exception:
                pass

        texts = ["turn light on", "turn light off", "turn fan on", "turn fan off", "set auto"]
        labels = ["LED_ON", "LED_OFF", "FAN_ON", "FAN_OFF", "FAN_AUTO"]
        self.vec = TfidfVectorizer()
        Xv = self.vec.fit_transform(texts)
        try:
            self.clf = SGDClassifier(max_iter=1000, tol=1e-3)
            self.clf.partial_fit(Xv, labels, classes=list(set(labels)))
        except Exception as e:
            self._log("classifier bootstrap failed:", e)
            self.clf = SGDClassifier(max_iter=1000, tol=1e-3)
            try:
                self.clf.fit(Xv, labels)
            except Exception:
                pass

        if os.path.exists(self.model_path):
            try:
                self.load(self.model_path)
                self._log("Loaded models from", self.model_path)
            except Exception as e:
                self._log("Failed to load model file:", e)

    def predict_fan(self, temp, hum, led_state, pir):
        """Synchronous prediction (0..255 int). Thread-safe read."""
        with self._lock:
            try:
                feat = np.array([[float(temp), float(hum), 1.0 if led_state else 0.0, 1.0 if pir else 0.0]])
                Xs = self.scaler.transform(feat)
                pred = float(self.reg.predict(Xs)[0])
                val = int(np.clip(np.round(pred), 0, 255))
                return val
            except Exception as e:
                self._log("predict_fan error:", e)
                return 0

    def predict_intent(self, text):
        """Return predicted label from classifier. If classifier not ready, return None."""
        with self._lock:
            try:
                xv = self.vec.transform([text])
                return self.clf.predict(xv)[0]
            except Exception as e:
                self._log("predict_intent error:", e)
                return None

    def update_regressor(self, temp, hum, led_state, pir, fan_label, async_train=True):
        """Queue or run a partial_fit for the regressor. Scaler updated incrementally first."""
        feat = (float(temp), float(hum), 1.0 if led_state else 0.0, 1.0 if pir else 0.0)
        item = ("reg", feat, float(fan_label))
        if async_train:
            self._train_q.put(item)
        else:
            with self._lock:
                X = np.array([[feat[0], feat[1], feat[2], feat[3]]])
                try:
                    self.scaler.partial_fit(X)
                    Xs = self.scaler.transform(X)
                    self.reg.partial_fit(Xs, np.array([item[2]]))
                except Exception as e:
                    self._log("blocking update_regressor failed:", e)

    def update_intent(self, text, label, async_train=True):
        """Queue or run a partial_fit for the intent classifier (vectorizes text first)."""
        item = ("int", text, label)
        if async_train:
            self._train_q.put(item)
        else:
            with self._lock:
                try:
                    xv = self.vec.transform([text])
                    self.clf.partial_fit(xv, [label])
                except Exception as e:
                    self._log("blocking update_intent failed:", e)

    # ---------------- Trainer thread ----------------
    def _trainer_loop(self):
        self._log("trainer thread started")
        while not self._stop_evt.is_set():
            try:
                item = self._train_q.get(timeout=0.5)
            except Empty:
                continue
            try:
                if item[0] == "reg":
                    _, feat_tuple, label = item
                    X = np.array([[feat_tuple[0], feat_tuple[1], feat_tuple[2], feat_tuple[3]]])
                    y = np.array([label])
                    with self._lock:
                        try:
                            # update scaler incrementally then regressor
                            self.scaler.partial_fit(X)
                            Xs = self.scaler.transform(X)
                            self.reg.partial_fit(Xs, y)
                            self._log("trained reg on", X.tolist(), "->", y.tolist())
                        except Exception as e:
                            self._log("reg partial_fit error:", e)
                elif item[0] == "int":
                    _, text, label = item
                    with self._lock:
                        try:
                            xv = self.vec.transform([text])
                            self.clf.partial_fit(xv, [label])
                            self._log("trained intent on", text, "->", label)
                        except Exception as e:
                            self._log("intent partial_fit error:", e)
                else:
                    self._log("Unknown training record:", item)
            except Exception as e:
                self._log("trainer loop exception:", e)
        self._log("trainer thread stopping")

    def start(self):
        """Start background trainer thread."""
        if self._trainer and self._trainer.is_alive():
            return
        self._stop_evt.clear()
        self._trainer = Thread(target=self._trainer_loop, daemon=True)
        self._trainer.start()

    def stop(self):
        """Stop trainer thread and save models."""
        self._stop_evt.set()
        if self._trainer:
            self._trainer.join(timeout=2.0)
        try:
            self.save(self.model_path)
            self._log("models saved on stop")
        except Exception:
            pass

    # ---------------- Persistence ----------------
    def save(self, fname=None):
        fname = fname or self.model_path
        with self._lock:
            joblib.dump({'reg': self.reg, 'clf': self.clf, 'vec': self.vec, 'scaler': self.scaler}, fname)
        self._log("models saved to", fname)

    def load(self, fname=None):
        fname = fname or self.model_path
        if not os.path.exists(fname):
            raise FileNotFoundError(fname)
        with self._lock:
            d = joblib.load(fname)
            self.reg = d['reg']; self.clf = d['clf']; self.vec = d['vec']; self.scaler = d.get('scaler', self.scaler)
        self._log("models loaded from", fname)

default_ml = MLBrain(verbose=False)

if __name__ == "__main__":
    m = MLBrain(verbose=True)
    m.start()
    print("pred fan for 29C, 60%:", m.predict_fan(29,60,True,1))
    m.update_regressor(29,60,True,1,200.0, async_train=False)
    print("after update:", m.predict_fan(29,60,True,1))
    m.stop()
