from flask import Flask, render_template_string, request, Response, stream_with_context
import time, socket, json, queue, threading

app = Flask(__name__)
state = {
    "temp": 0.0,
    "hum": 0.0,
    "pir": 0,
    "smoke": 0,
    "led": False,
    "fan": 0,
    "led_mode": "auto",
    "fan_mode": "auto"
}

ACTION_LOG = []
VOICE_BUFFER = []
VOICE_BUFFER_MAX = 4
START_TIME = time.time()

# SSE subscribers
_subscribers = []
_sub_lock = threading.Lock()

_controller_callback = None

def set_controller_callback(callback_fn):
    """Allow main.py to inject the controller's apply_intent method."""
    global _controller_callback
    _controller_callback = callback_fn
    print("[flask_app] controller callback set")


def add_subscriber(q):
    with _sub_lock:
        _subscribers.append(q)

def remove_subscriber(q):
    with _sub_lock:
        try:
            _subscribers.remove(q)
        except ValueError:
            pass

def publish(event_name, data):
    payload = {"event": event_name, "data": data}
    with _sub_lock:
        for q in list(_subscribers):
            try:
                q.put(payload, block=False)
            except queue.Full:
                pass


INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>Vesta — Smart Home Dashboard</title>
  <style>
    :root{
      --bg: linear-gradient(180deg,#071021 0%,#0b1b2b 100%);
      --card:#081826;
      --muted:#8aa0b0;
      --accent:#00d2ff;
      --accent-2:#7b61ff;
      --glass: rgba(255,255,255,0.04);
      --danger:#ff5c5c;
      --ok:#46ffb3;
      --shadow: 0 8px 30px rgba(2,6,23,0.6);
      font-family: Inter, system-ui, -apple-system, 'Segoe UI', Roboto, 'Helvetica Neue', Arial;
    }
    html,body{height:100%;margin:0;background:var(--bg);color:#e6f0f6}
    .wrap{max-width:1100px;margin:18px auto;padding:18px}
    header{display:flex;align-items:center;justify-content:space-between}
    .brand{display:flex;gap:12px;align-items:center}
    .logo{width:56px;height:56px;border-radius:12px;background:linear-gradient(135deg,var(--accent),var(--accent-2));box-shadow:0 6px 18px rgba(123,97,255,0.18);display:flex;align-items:center;justify-content:center;font-weight:700}
    h1{font-size:20px;margin:0}
    p.sub{color:var(--muted);margin:0}

    .grid{display:grid;grid-template-columns:repeat(12,1fr);gap:16px;margin-top:18px}
    .card{grid-column:span 4;background:linear-gradient(180deg, rgba(255,255,255,0.02), rgba(255,255,255,0.01));border-radius:14px;padding:14px;box-shadow:var(--shadow);backdrop-filter: blur(6px);border:1px solid rgba(255,255,255,0.03)}
    .card.wide{grid-column:span 8}
    .card.header{display:flex;flex-direction:row;align-items:center;justify-content:space-between}

    .metric{font-size:28px;font-weight:600}
    .muted{color:var(--muted);font-size:13px}

    .toggle{display:inline-flex;align-items:center;gap:8px}
    .btn{background:var(--glass);border:1px solid rgba(255,255,255,0.04);padding:8px 12px;border-radius:10px;color:var(--muted);cursor:pointer}
    .btn.primary{background:linear-gradient(90deg,var(--accent),var(--accent-2));color:#001018;font-weight:700;border:none}
    .btn.active{background:linear-gradient(90deg,var(--ok),#51ffd8);color:#00211a;border:none;box-shadow:0 10px 30px rgba(70,255,179,0.08)}
    .control-row{display:flex;gap:8px;flex-wrap:wrap}

    .big{
      display:flex;align-items:center;justify-content:space-between;gap:12px
    }
    .status-pill{padding:8px 12px;border-radius:999px;background:rgba(255,255,255,0.02);color:var(--muted);display:inline-flex;gap:8px;align-items:center}
    .rad{width:86px;height:86px;border-radius:20px;background:linear-gradient(135deg, rgba(255,255,255,0.02), rgba(255,255,255,0.01));display:flex;align-items:center;justify-content:center;flex-direction:column}

    .log{max-height:240px;overflow:auto;padding:8px;background:rgba(255,255,255,0.01);border-radius:10px;border:1px solid rgba(255,255,255,0.02)}
    .log-item{padding:6px 8px;border-bottom:1px dashed rgba(255,255,255,0.02);font-size:13px}

    .voice-list{display:flex;flex-direction:column;gap:6px; max-height: 150px; overflow: auto;}
    .voice-item{padding:8px;border-radius:10px;background:rgba(255,255,255,0.01);border:1px solid rgba(255,255,255,0.02);font-size:13px}
    .voice-text{font-weight:600}
    .voice-intent{color:var(--muted);font-size:12px}

    @media (max-width:900px){
      .card{grid-column:span 12}
      .card.wide{grid-column:span 12}
      .brand h1{font-size:18px}
    }

    .glow{box-shadow:0 6px 30px rgba(0,210,255,0.08), 0 0 18px rgba(123,97,255,0.04) inset}

    input[type=range]{width:100%}

    .small-pill{padding:6px 8px;border-radius:999px;background:rgba(255,255,255,0.02);display:inline-block}

    progress {
      -webkit-appearance: none;
      appearance: none;
      width: 100%;
      height: 6px;
      border: none;
      border-radius: 999px;
      overflow: hidden;
      background: rgba(255,255,255,0.03);
    }
    progress::-webkit-progress-bar {
      background: rgba(255,255,255,0.03);
      border-radius: 999px;
    }
    progress::-webkit-progress-value {
      background: var(--ok);
      border-radius: 999px;
      transition: all 0.1s linear;
    }
    progress::-moz-progress-bar {
      background: var(--ok);
      border-radius: 999px;
      transition: all 0.1s linear;
    }
  </style>
</head>
<body>
  <div class="wrap">
    <header>
      <div class="brand">
        <div class="logo">V</div>
        <div>
          <h1>Vesta — Smart Home</h1>
          <p class="sub">Offline local dashboard • Live sensors • Voice & Dashboard override</p>
        </div>
      </div>
      <div style="text-align:right">
        <div class="status-pill" id="conn">● Offline</div>
        <div class="muted">Host: <span id="host">local</span></div>
      </div>
    </header>

    <div class="grid">
      <div class="card header wide">
        <div class="big">
          <div>
            <div class="muted">Temperature</div>
            <div class="metric" id="temp">-- °C</div>
            <div class="muted">Humidity <span id="hum">-- %</span></div>
          </div>
          <div>
            <div class="rad glow" id="fanRad">
              <div id="fanVal">0</div>
              <div class="muted">FAN PWM</div>
            </div>
          </div>
          <div style="min-width:140px">
            <div style="display:flex;gap:8px;align-items:center">
              <div class="muted">LED</div>
              <div id="ledModePill" class="small-pill">Auto</div>
            </div>
            <div id="ledState" class="metric">OFF</div>
            <div style="margin-top:8px;display:flex;gap:8px;align-items:center">
              <div class="muted">Smoke</div>
              <div id="smokeState" class="small-pill">SAFE</div>
            </div>
          </div>
        </div>
        <div style="display:flex;flex-direction:column;gap:8px;align-items:flex-end">
          <div class="control-row">
            <button id="btnLedAuto" class="btn" onclick="send('LED_AUTO')">Auto LED</button>
            <button id="btnLedOn" class="btn" onclick="send('LED_ON')">LED ON</button>
            <button id="btnLedOff" class="btn" onclick="send('LED_OFF')">LED OFF</button>
          </div>
          <div class="control-row">
            <button id="btnFanAuto" class="btn" onclick="send('FAN_AUTO')">Auto Fan</button>
            <button id="btnFanOn" class="btn" onclick="send('FAN_ON')">Fan ON</button>
            <button id="btnFanOff" class="btn" onclick="send('FAN_OFF')">Fan OFF</button>
          </div>
          <div style="width:220px;text-align:center">
            <div class="muted">Quick Mode</div>
            <select id="quickMode" class="btn" onchange="onQuick(this.value)">
              <option value="">Manual</option>
              <option value="comfort">Comfort</option>
              <option value="eco">Eco</option>
              <option value="boost">Boost</option>
            </select>
          </div>
        </div>
      </div>

      <div class="card">
        <div class="muted">Live Sensor Data</div>
        <div class="sensor-list" style="margin-top: 8px; display: grid; grid-template-columns: 1fr 1fr; gap: 8px; height: 150px;">
          <div class="sensor-item">
            <div class="muted" style="font-size: 11px;">MOTION (PIR)</div>
            <div id="sensorPIR" class="metric" style="font-size: 18px; color: var(--muted);">-</div>
          </div>
          <div class="sensor-item">
            <div class="muted" style="font-size: 11px;">SMOKE</div>
            <div id="sensorSmoke" class="metric" style="font-size: 18px; color: var(--muted);">-</div>
          </div>
          <div class="sensor-item">
            <div class="muted" style="font-size: 11px;">LED STATE</div>
            <div id="sensorLED" class="metric" style="font-size: 18px; color: var(--muted);">-</div>
          </div>
          <div class="sensor-item">
            <div class="muted" style="font-size: 11px;">FAN PWM</div>
            <div id="sensorFan" class="metric" style="font-size: 18px; color: var(--muted);">-</div>
          </div>
        </div>
      </div>

      <div class="card">
        <div class="muted">Voice Activity</div>
        
        <div class="muted" style="margin-top:8px; font-size: 11px;">Mic Activity</div>
        <progress id="micProgress" max="1000" value="0"></progress>
        
        <div class="muted" style="margin-top:12px;">Recent inputs (live)</div>
        <div style="margin-top:8px" class="voice-list" id="voiceList">
        </div>
      </div>

      <div class="card">
        <div class="muted">Fan Control</div>
        <div style="margin-top:8px">
          <input id="fanSlider" type="range" min="0" max="255" value="0" oninput="onFanSlide(this.value)">
          <div class="muted">PWM: <span id="fanSliderVal">0</span></div>
          <button id="btnSetPWM" class="btn primary" onclick="sendPWM()">Set PWM</button>
        </div>
      </div>

      <div class="card">
        <div class="muted">System</div>
        <div style="margin-top:8px">
          <div>IP: <span id="ipAddr">-</span></div>
          <div>Uptime: <span id="uptime">-</span></div>
          <div style="margin-top:8px"><button class="btn" onclick="clearLog()">Clear Log</button></div>
        </div>
      </div>

      <div class="card wide">
        <div class="muted">Activity Log (Recent 5)</div>
        <div class="log" id="activity"></div>
      </div>
    </div>

    <div style="height:24px"></div>
    <footer style="text-align:center;color:var(--muted)">Vesta — Offline local dashboard • Access from your phone at <span id="hint"></span></footer>
  </div>

  <script>
    const stateEl = (id)=>document.getElementById(id);

    function updateButtons(s){
      document.getElementById('btnLedOn').classList.toggle('active', !!s.led && s.led_mode==='manual');
      document.getElementById('btnLedOff').classList.toggle('active', !s.led && s.led_mode==='manual');
      document.getElementById('btnLedAuto').classList.toggle('active', s.led_mode==='auto');
      document.getElementById('btnFanOn').classList.toggle('active', s.fan && s.fan>0 && s.fan_mode==='manual');
      document.getElementById('btnFanOff').classList.toggle('active', s.fan===0 && s.fan_mode==='manual');
      document.getElementById('btnFanAuto').classList.toggle('active', s.fan_mode==='auto');
      
      document.getElementById('ledModePill').innerText = (s.led_mode||'auto').toUpperCase();
      document.getElementById('smokeState').innerText = s.smoke? 'SMOKE' : 'SAFE';
      document.getElementById('smokeState').style.color = s.smoke? 'var(--danger)':'var(--muted)';
    }

    let es = null;
    function connectSSE(){
      es = new EventSource('/events/stream');

      es.addEventListener('open', function(){
        stateEl('conn').innerText = '● Connected';
        stateEl('conn').style.color = '#46ffb3';
      });

      es.addEventListener('error', function(){
        stateEl('conn').innerText = '● Offline';
        stateEl('conn').style.color = '#ff9aa2';
      });

      es.addEventListener('state', function(e){
        const d = JSON.parse(e.data);
        
        stateEl('temp').innerText = (d.temp===null? '--' : d.temp.toFixed(1)+' °C');
        stateEl('hum').innerText = (d.hum===null? '--' : d.hum.toFixed(0)+' %');
        stateEl('fanVal').innerText = d.fan || 0;
        stateEl('fanSliderVal').innerText = d.fan || 0;
        document.getElementById('fanSlider').value = d.fan || 0;
        stateEl('ledState').innerText = (d.led? 'ON' : 'OFF');
        stateEl('smokeState').innerText = (d.smoke? 'SMOKE' : 'SAFE');

        updateButtons(d);
        
        const sensorPIR = stateEl('sensorPIR');
        const sensorSmoke = stateEl('sensorSmoke');
        const sensorLED = stateEl('sensorLED');
        const sensorFan = stateEl('sensorFan');

        if (sensorPIR) {
          sensorPIR.innerText = d.pir ? 'MOTION' : 'Clear';
          sensorPIR.style.color = d.pir ? 'var(--accent)' : 'var(--muted)';
        }
        if (sensorSmoke) {
          sensorSmoke.innerText = d.smoke ? 'DETECTED' : 'Clear';
          sensorSmoke.style.color = d.smoke ? 'var(--danger)' : 'var(--muted)';
        }
        if (sensorLED) {
          sensorLED.innerText = d.led ? 'ON' : 'OFF';
          sensorLED.style.color = d.led ? 'var(--ok)' : 'var(--muted)';
        }
        if (sensorFan) {
          sensorFan.innerText = d.fan || 0;
          sensorFan.style.color = d.fan > 0 ? 'var(--accent-2)' : 'var(--muted)';
        }
        
        var act = document.getElementById('activity');
        var li = document.createElement('div'); li.className='log-item';
        li.innerText = new Date().toLocaleTimeString() + ' • state updated (T:'+d.temp+', H:'+d.hum+', P:'+d.pir+')';
        act.prepend(li);
        
        while(act.children.length > 5) act.removeChild(act.lastChild);
      });

      es.addEventListener('voice_event', function(e){
        var entry = JSON.parse(e.data); // {text, intent, time}
        var voiceList = document.getElementById('voiceList');
        var item = document.createElement('div'); item.className='voice-item';
        
        var t = document.createElement('div'); 
        t.className='voice-text'; 
        t.innerText = entry.text;
        if (entry.text.startsWith('You:')) {
            t.style.color = 'var(--accent)';
        } else if (entry.text.startsWith('Vista:')) {
            t.style.color = 'var(--ok)';
        }
        
        var it = document.createElement('div'); 
        it.className='voice-intent'; 
        it.innerText = (entry.intent? 'Intent: '+entry.intent : '') + ' • ' + (entry.time || new Date().toLocaleTimeString());
        
        item.appendChild(t); 
        item.appendChild(it);
        voiceList.prepend(item);
        
        while(voiceList.children.length > 4) voiceList.removeChild(voiceList.lastChild);
      });

      es.addEventListener('action_ack', function(e){
        var act = document.getElementById('activity');
        var now = new Date().toLocaleTimeString();
        var li = document.createElement('div'); li.className='log-item';
        li.innerText = now + ' • ' + e.data;
        act.prepend(li);
        
        while(act.children.length > 5) act.removeChild(act.lastChild);
      });

      es.addEventListener('uptime', function(e){
        document.getElementById('uptime').innerText = e.data;
      });

      es.addEventListener('voice_rms', function(e){
        const level = JSON.parse(e.data);
        const progBar = stateEl('micProgress');
        if (progBar) {
            progBar.value = Math.min(level, 1000); 
        }
      });
    }

    connectSSE();

    function send(cmd){
      fetch('/command', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({cmd})
      }).catch(()=>{});
    }

    function onFanSlide(v){ document.getElementById('fanSliderVal').innerText = v; }
    function sendPWM(){
      var v = document.getElementById('fanSlider').value;
      fetch('/command', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({cmd:'FAN_PWM:'+v})});
    }

    function onQuick(mode){
      if(!mode) return; fetch('/command',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({cmd:'QUICK:'+mode})});
      document.getElementById('quickMode').value='';
    }

    function clearLog(){ document.getElementById('activity').innerHTML=''; }

    fetch('/_local_ip').then(r=>r.text()).then(ip=>{
      document.getElementById('ipAddr').innerText = ip;
      document.getElementById('hint').innerText = ip + ':5000';
      document.getElementById('host').innerText = ip;
    }).catch(()=>{});

    setInterval(()=>fetch('/_ping'), 30000);
  </script>
</body>
</html>
"""

@app.route('/')
def index():
    return render_template_string(INDEX_HTML)

@app.route('/_local_ip')
def get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
    except Exception:
        ip = request.host.split(':')[0]
    return ip

@app.route('/_ping')
def ping():
    return ('', 204)

@app.route('/command', methods=['POST'])
def command():
    data = request.get_json(force=True)
    cmd = data.get('cmd') if data else None
    if not cmd:
        return {'ok': False, 'error': 'no cmd'}, 400

    print("dashboard cmd received:", cmd)
    ACTION_LOG.insert(0, f"{time.strftime('%H:%M:%S')} - {cmd}")

    if _controller_callback:
        try:
            _controller_callback(cmd, text=cmd, source='dashboard')
        except Exception as e:
            print(f"[flask_app] error calling controller callback: {e}")
            return {'ok': False, 'error': str(e)}, 500
    else:
        print("[flask_app] ERROR: Controller callback not set. Dashboard commands will not work.")
        return {'ok': False, 'error': 'controller_not_connected'}, 500
    
    publish('action_ack', f"Dashboard command: {cmd}")
    return {'ok': True}

@app.route('/events/stream')
def stream_events():
    q = queue.Queue(maxsize=256)
    add_subscriber(q)

    def gen():
        yield "event: state\n"
        yield f"data: {json.dumps(state)}\n\n"

        last_hb = time.time()
        try:
            while True:
                try:
                    item = q.get(timeout=1.0)
                    ev = item.get('event')
                    data = item.get('data')
                    yield f"event: {ev}\n"
                    yield f"data: {json.dumps(data)}\n\n"
                except queue.Empty:
                    if time.time() - last_hb > 15:
                        yield ": heartbeat\n\n"
                        last_hb = time.time()
                        uptime = int(time.time() - START_TIME)
                        yield f"event: uptime\n"
                        yield f"data: {uptime}\n\n"
                    continue
        finally:
            remove_subscriber(q)

    return Response(stream_with_context(gen()), mimetype="text/event-stream")

def emit_voice(text, intent=None):
    entry = {'text': text, 'intent': intent, 'time': time.strftime('%H:%M:%S')}
    VOICE_BUFFER.insert(0, entry)
    if len(VOICE_BUFFER) > VOICE_BUFFER_MAX:
        VOICE_BUFFER.pop()
    publish('voice_event', entry)

def publish_rms(level):
    """Publishes the microphone RMS level."""
    publish('voice_rms', level)

def update_state(new):
    """
    This is called by the CONTROLLER to update the dashboard's state
    and broadcast it to all clients.
    """
    global state
    state.update(new)
    if 'led_mode' not in state: state['led_mode'] = 'auto'
    if 'fan_mode' not in state: state['fan_mode'] = 'auto'
    publish('state', state)

if __name__ == '__main__':
    print("Starting Vesta SSE dashboard on http://0.0.0.0:5000")
    app.run(host='0.0.0.0', port=5000, threaded=True)