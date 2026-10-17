from functools import wraps
from flask import Flask, request, jsonify, render_template
from qa_store import add_qa, load_qa, save_qa
from face_utils import register_face, list_faces, delete_face
from werkzeug.utils import secure_filename
from settings import load_settings, save_settings
from flask_cors import CORS
from identity import load_identity
import control_session
import os, threading, serial, serial.tools.list_ports, json, time, subprocess

# ── Q&A id mapping ─────────────────────────────────────────
# The new app addresses Q&A entries by id, but qa_store.json is keyed by
# question text and find_answer() depends on that. So the ids live here
# instead, and qa_store.json is left in exactly the shape main.py expects.
QA_IDS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "qa_ids.json")


def _load_qa_ids():
    try:
        with open(QA_IDS_FILE) as f:
            d = json.load(f)
            return {str(k): str(v) for k, v in d.items()}
    except Exception:
        return {}


def _save_qa_ids(ids):
    tmp = QA_IDS_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(ids, f, indent=2)
    os.replace(tmp, QA_IDS_FILE)      # atomic: a crash cannot truncate it


# ── Pose Storage ───────────────────────────────────────────
POSES_FILE = "poses.json"

def load_poses():
    if os.path.exists(POSES_FILE):
        with open(POSES_FILE, 'r') as f:
            return json.load(f)
    return {}

def save_poses(poses):
    with open(POSES_FILE, 'w') as f:
        json.dump(poses, f, indent=2)

# ── Flask App ──────────────────────────────────────────────
app = Flask(__name__, template_folder='templates')
CORS(app)

UPLOAD_FOLDER = "faces"
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# ── Face Model Init ────────────────────────────────────────
try:
    from face_utils import app as face_app
    print("[FACE] Model initialized")
except Exception as e:
    print(f"[WARNING] Face model init failed: {e}")

# ── ESP32 Serial ───────────────────────────────────────────
try:
    esp32 = serial.Serial('/dev/ttyAMA0', 115200, timeout=1)
    print(f"[ESP32] Connected on /dev/ttyAMA0")
except Exception as e:
    esp32 = None
    print(f"[ESP32] Not connected: {e}")

def reconnect_esp32():
    global esp32
    try:
        if esp32: esp32.close()
        esp32 = serial.Serial('/dev/ttyAMA0', 115200, timeout=1)
        print(f"[ESP32] Reconnected on /dev/ttyAMA0")
        return True
    except:
        esp32 = None
        return False

_esp_lock = threading.Lock()


def send_to_esp32(command: str, quiet: bool = False):
    """Write one line to the ESP32.

    Serialised with a lock, because several threads now write to this port:
    the movement keepalive, the loop sequencer, the ESP32 reader's reconnect
    and every Flask request thread. Two interleaved writes produce a corrupted
    line that the firmware discards — and nothing reports it.

    quiet suppresses the log line. The keepalive sends three commands a second
    while a button is held, which would otherwise bury everything else in the
    journal.
    """
    global esp32
    with _esp_lock:
        try:
            if esp32 and esp32.is_open:
                esp32.write((command + '\n').encode())
                if not quiet:
                    print(f"[ESP32] Sent: {command}")
            else:
                print(f"[ESP32] Reconnecting...")
                if reconnect_esp32():
                    esp32.write((command + '\n').encode())
        except Exception as e:
            print(f"[ESP32] Error: {e}")
            reconnect_esp32()


# ══════════════════════════════════════════════════════════════════════
# VOLUME
#
# The app sends the slider as a QUERY PARAMETER on a GET to /settings:
#
#     GET /settings?robot=giya&tier=max&volume=66&session_id=...   200
#
# That route used to ignore request.args entirely and just return the stored
# settings, so the robot answered 200, the app believed the volume was set,
# and nothing happened — the same silent-success failure as the
# /obstacle_avoidance 404.
#
# And even once the value is stored, nothing in audio_utils.py touches the
# ALSA mixer, so aplay would still play at whatever level the card booted at.
# Storing it is only half the job; this applies it.
#
# The card is resolved through get_speaker_device() rather than hardcoded,
# because card numbers shuffle between boots on this robot — the same reason
# the shutdown .wav was playing into the wrong device.
# ══════════════════════════════════════════════════════════════════════

# Control name differs per card: the PCM2902 USB dongle usually exposes
# 'Speaker' or 'PCM', the voiceHAT 'Master'. First one that exists wins.
_MIXER_CONTROLS = ('Speaker', 'PCM', 'Master', 'Headphone')


def apply_volume(percent):
    """Set playback volume on whichever card is actually the speaker."""
    try:
        percent = max(0, min(100, int(float(percent))))
    except (TypeError, ValueError):
        print(f"[VOLUME] ignoring non-numeric value {percent!r}")
        return False

    try:
        from audio_utils import get_speaker_device
        dev = get_speaker_device()
        card = dev.split(':')[1].split(',')[0]
    except Exception as e:
        print(f"[VOLUME] could not resolve the speaker card: {e}")
        return False

    for control in _MIXER_CONTROLS:
        try:
            r = subprocess.run(['amixer', '-c', card, 'sset', control,
                                f'{percent}%', 'unmute'],
                               capture_output=True, timeout=5)
        except Exception as e:
            print(f"[VOLUME] amixer failed: {e}")
            return False
        if r.returncode == 0:
            print(f"[VOLUME] card {card} {control} -> {percent}%")
            return True

    # Worth saying loudly rather than failing quietly: some cheap PCM2902
    # dongles expose only a CAPTURE control and no playback control at all,
    # in which case amixer cannot help and a softvol plugin in ~/.asoundrc is
    # the only route.
    print(f"[VOLUME] no usable playback control on card {card} — check: "
          f"amixer -c {card} scontrols")
    return False


# ── Speak Welcome ──────────────────────────────────────────
def speak_welcome():
    from audio_utils import speak
    try:
        from eyes import set_state
        set_state("person")
        time.sleep(1)
        set_state("speaking")
    except: pass
    s = load_settings()
    welcome = s.get('welcome_speech', 'Hello welcome!')
    speak(welcome)
    try:
        from eyes import set_state
        set_state("idle")
    except: pass
    send_to_esp32("RESUME")
    print("[ESP32] Resume sent after welcome")

# ── Shared Shutdown ────────────────────────────────────────
def do_shutdown():
    print("[SHUTDOWN] Step 1 - starting")
    try:
        from eyes import set_state
        set_state("obstacle")
    except: pass
    print("[SHUTDOWN] Step 2 - stopping motors")
    try:
        send_to_esp32("MOVE:stop")
        time.sleep(1)
    except: pass
    print("[SHUTDOWN] Step 3 - homing")
    try:
        send_to_esp32("HOME")
        time.sleep(2)
    except: pass
    print("[SHUTDOWN] Step 4 - playing audio")
    try:
        shutdown_wav = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                    "shutdown.wav")
        if os.path.exists(shutdown_wav):
            # Was hardcoded to plughw:2,0. The speaker is on card 3 on this
            # robot, and card numbers shuffle between boots anyway  so the
            # goodbye was playing into whatever happened to be card 2.
            from audio_utils import get_speaker_device
            subprocess.run(['aplay', '-q', '-D', get_speaker_device(),
                            shutdown_wav], timeout=8, capture_output=True)
        print("[SHUTDOWN] Step 4 done - audio played")
    except Exception as e:
        print(f"[SHUTDOWN] Step 4 error: {e}")
    print("[SHUTDOWN] Step 5 - sending LATCH:OFF")
    send_to_esp32("LATCH:OFF")

    # Step 6: call shutdown DIRECTLY.
    #
    # The old version did:
    #     subprocess.Popen(["bash","-c","sleep 2 && sudo /sbin/shutdown -h now"])
    #     os._exit(0)
    # which looks safer but is not. os._exit kills this process, systemd then
    # tears down the whole cgroup by default, and the "sleep 2" child dies
    # before it ever runs. The journal showed the result: "Deactivated
    # successfully" followed by "Scheduled restart job"  the service just
    # came back while the ESP32 cut power 15 seconds later on a running
    # machine. That is an unclean power-off on every button press.
    #
    # `shutdown -h now` hands off to systemd and returns immediately, so this
    # does not block either.
    print("[SHUTDOWN] Step 6 - poweroff")
    subprocess.run(["sudo", "/sbin/shutdown", "-h", "now"])

# ── ESP32 Reader ───────────────────────────────────────────
hardware_enabled = True

def esp32_reader():
    print("[ESP32] Starting reader thread")
    last_data = time.time()
    while True:
        try:
            if esp32 and esp32.is_open:
                if esp32.in_waiting:
                    line = esp32.readline().decode('utf-8', errors='ignore').strip()
                    if not line:
                        continue
                    last_data = time.time()
                    print(f"[ESP32] << {line}")
                    if line.startswith("PERSON_DETECTED:"):
                        dist = line.split(":")[1]
                        print(f"[ESP32] Person at {dist}cm")
                        if hardware_enabled:
                            threading.Thread(target=speak_welcome, daemon=True).start()
                        else:
                            print("[HARDWARE] Disabled skipping welcome")
                    elif line.startswith("OBSTACLE:"):
                        print(f"[ESP32] Obstacle detected")
                        if hardware_enabled:
                            try:
                                from eyes import set_state
                                set_state("obstacle")
                            except: pass
                            threading.Thread(target=speak_welcome, daemon=True).start()
                        else:
                            print("[HARDWARE] Disabled ignoring obstacle")
                    elif line.startswith("CLEAR:"):
                        print(f"[ESP32] Clear")
                        try:
                            from eyes import set_state
                            set_state("forward")
                        except: pass

                    # ── BUTTON SHUTDOWN FROM ESP32 ─────────────
                    elif line.startswith("SHUTDOWN"):
                        print("[ESP32] Button shutdown received!")
                        threading.Thread(target=do_shutdown, daemon=True).start()

                    elif any(x in line for x in ["ready","READY","Giya","BLOCKED","LATCH","HW:"]):
                        print(f"[ESP32] {line}")

                if time.time() - last_data > 120:
                    print("[ESP32] No data 120s — reconnecting...")
                    reconnect_esp32()
                    last_data = time.time()
        except Exception as e:
            print(f"[ESP32] Reader error: {e}")
            reconnect_esp32()
            last_data = time.time()
        time.sleep(0.05)

esp32_reader_thread = threading.Thread(target=esp32_reader, daemon=True)
esp32_reader_thread.start()

# ── Mega Serial ────────────────────────────────────────────
try:
    mega = serial.Serial('/dev/mega', 9600, timeout=1)
    print(f"[MEGA] Connected on /dev/mega")
except Exception as e:
    mega = None
    print(f"[MEGA] Not connected: {e}")

def reconnect_mega():
    global mega
    try:
        if mega: mega.close()
        mega = serial.Serial('/dev/mega', 9600, timeout=1)
        print(f"[MEGA] Reconnected on /dev/mega")
        return True
    except:
        mega = None
        return False

def send_to_mega(command: str):
    global mega
    try:
        if mega and mega.is_open:
            mega.write((command + '\n').encode())
            print(f"[MEGA] Sent: {command}")
        else:
            print(f"[MEGA] Reconnecting...")
            if reconnect_mega():
                mega.write((command + '\n').encode())
    except Exception as e:
        print(f"[MEGA] Error: {e}")
        reconnect_mega()

# ── Loop Control ───────────────────────────────────────────
loop_running = False
loop_thread  = None

def run_loop():
    global loop_running
    poses = load_poses()
    if not poses:
        print("[LOOP] No poses saved")
        return
    print(f"[LOOP] Starting with {len(poses)} poses")
    while loop_running:
        for pos_num in sorted(poses.keys(), key=int):
            if not loop_running:
                break
            pose   = poses[pos_num]
            hand   = pose.get('hand', 'left')
            servos = pose.get('servos', {})
            print(f"[LOOP] Playing pose {pos_num} hand={hand}")
            for part, value in servos.items():
                send_to_esp32(f"POS:{part}:{value}:{hand}")
                time.sleep(0.05)
            time.sleep(1)
    print("[LOOP] Stopped")

# ── Dashboard ──────────────────────────────────────────────
@app.route('/')
def index():
    return render_template('index.html')


# ══════════════════════════════════════════════════════════════════════
# CONTROL SESSION  (protocol v1)
#
# One controller at a time, enforced locally with no cloud dependency. The
# session lives in memory in control_session.py; a restart clears it, which is
# deliberate — a crash can never leave the robot falsely locked.
#
# STRICT from the start on Giya, unlike Luna. Luna has an on-robot touchscreen
# that calls these routes directly and sends no session_id, so it needed a
# loopback exemption and a permissive transition period. Giya has no display:
# the phone app is the only client, and it already implements
# acquire/heartbeat, so the single-controller guarantee can hold properly from
# day one.
#
# If anything else is ever pointed at this robot — a joystick script, a test
# tool — it will need a session too, or it gets 423.
# ══════════════════════════════════════════════════════════════════════

def require_control_session(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        session = control_session.get_active_session()
        if session is None:
            return jsonify({"status": "error",
                            "message": "no_active_control_session"}), 423
        body = request.get_json(silent=True) or {}
        provided = request.args.get('session_id') or body.get('session_id')
        if provided != session['session_id']:
            return jsonify({"status": "error",
                            "message": "invalid_or_foreign_session"}), 423
        return f(*args, **kwargs)
    return wrapper


@app.route('/control/acquire', methods=['POST', 'GET'])
def control_acquire():
    body = request.get_json(silent=True) or {}
    app_id = (request.args.get('app_id') or body.get('app_id') or '').strip().lower()
    client_id = (request.args.get('client_id') or body.get('client_id') or '').strip()

    if not app_id or not client_id:
        return jsonify({"status": "error",
                        "message": "app_id and client_id are required"}), 400

    # The app-to-robot check, which is the point of the protocol: a Luna or
    # Cabi app can never take control of a Giya unit.
    identity = load_identity()
    robot_type = (identity or {}).get('robot_type', '').strip().lower()
    if robot_type and app_id != robot_type:
        print(f"[CONTROL] rejected — app_id '{app_id}' != robot_type "
              f"'{robot_type}'")
        return jsonify({"status": "error",
                        "message": "app_identity_mismatch"}), 403

    ok, session = control_session.acquire(app_id, client_id)
    if not ok:
        return jsonify({"status": "error", "message": "robot_busy"}), 409

    print(f"[CONTROL] acquired — client={client_id} app={app_id} "
          f"session={session['session_id']}")
    return jsonify({
        "status": "ok",
        "session_id": session['session_id'],
        "timeout_seconds": control_session.CONTROL_SESSION_TIMEOUT_SECONDS
    }), 200


@app.route('/control/heartbeat', methods=['POST', 'GET'])
def control_heartbeat():
    body = request.get_json(silent=True) or {}
    session_id = request.args.get('session_id') or body.get('session_id')
    if not session_id or not control_session.heartbeat(session_id):
        # The session is gone. Almost always one of two things: the robot
        # restarted (sessions live in memory and are deliberately cleared, so
        # a crash can never leave the robot falsely locked), or the app went
        # quiet for longer than the timeout.
        #
        # Either way the app must call /control/acquire again. Saying so
        # explicitly, because an app that simply keeps retrying the heartbeat
        # stays locked out of every control route until it is restarted — and
        # from the outside that looks like the robot ignoring it.
        return jsonify({"status": "error", "message": "invalid_session",
                        "action": "reacquire",
                        "hint": "call /control/acquire to get a new "
                                "session_id"}), 423
    return jsonify({"status": "ok",
                    "timeout_seconds":
                        control_session.CONTROL_SESSION_TIMEOUT_SECONDS}), 200


@app.route('/control/release', methods=['POST', 'GET'])
def control_release():
    body = request.get_json(silent=True) or {}
    session_id = request.args.get('session_id') or body.get('session_id')
    if not session_id or not control_session.release(session_id):
        return jsonify({"status": "error", "message": "invalid_session"}), 423
    print(f"[CONTROL] released — session={session_id}")
    return jsonify({"status": "ok"}), 200


# ── Health ─────────────────────────────────────────────────
@app.route('/ping', methods=['GET'])
def ping():
    return jsonify({"status": "ok", "message": "Pi is alive"})

@app.route('/status', methods=['GET'])
def status():
    response = {
        "status": "ok",
        "battery": 100,          # no battery sensor on this robot
        "wifi": True,
        # Was hardcoded True. Now reports whether the serial port is actually
        # open, so an unplugged ESP32 shows as disconnected instead of the app
        # believing everything is fine.
        "connected": bool(esp32 and getattr(esp32, "is_open", False)),
    }

    # Control-session state. Booleans and counters only — the session_id is a
    # bearer token, and anyone able to read /status on the shared Wi-Fi could
    # otherwise capture it and impersonate the controller. It is returned
    # exactly once, to the client that just succeeded at /control/acquire.
    active = control_session.get_active_session()
    response["controller_active"] = active is not None
    response["session_expires_in"] = control_session.time_remaining()

    # Factory identity, merged from robot_identity.json. Omitted ENTIRELY when
    # the file is absent — never a placeholder — so the app cannot mistake an
    # unconfigured unit for an identified one.
    identity = load_identity()
    if identity:
        response.update(identity)
    return jsonify(response), 200

# ── Q&A ────────────────────────────────────────────────────
@app.route('/qa/add', methods=['POST'])
def add():
    """Add, update or delete Q&A entries.

    Accepts BOTH app formats:
        old app : [{"question": ..., "answer": ...}]
        new app : [{"id": "3", "q": ..., "a": ...}]
    and in the new one an entry whose q and a are both empty means "delete the
    entry with this id". Without that, deleting an answer in the app appeared
    to work while the answer stayed on the robot — worse than an error,
    because the customer believes they removed something they have not.

    ── WHY THE IDS LIVE IN A SEPARATE FILE ─────────────────────────────
    qa_store.json is FLAT:  {"who are you": "I am Giya"}

    and find_answer() depends on that shape completely — it does
    `spoken in qa` and then iterates expecting each value to be the answer
    STRING. Storing {id: {q, a}} instead would make every answer unfindable,
    and any entry that did match would hand a dict to speak().

    So the answers stay exactly as they are, and the id-to-question mapping
    the app needs is kept alongside in qa_ids.json. A delete looks the id up
    there to find which question to remove.
    """
    try:
        data = request.json
        items = data if isinstance(data, list) else [data]
        # Log what arrived. A delete that silently does nothing is impossible
        # to diagnose otherwise — the endpoint returns 200 either way.
        print(f"[QA] request: {json.dumps(data)[:400]}")

        qa = load_qa()                      # {question: answer}, flat
        ids = _load_qa_ids()                # {id: question}
        added = deleted = 0

        for item in items:
            if not isinstance(item, dict):
                continue
            qid = str(item.get('id', '')).strip()
            q = str(item.get('question', item.get('q', ''))).strip().lower()
            a = str(item.get('answer',   item.get('a', ''))).strip()

            # ── deletion ────────────────────────────────────────────
            # An entry with NO ANSWER is a deletion, whether or not the
            # question text came with it. The previous version required BOTH
            # to be empty, so {"id":"1","q":"hi","a":""} — which is what the
            # app sends when you clear a row — fell through to the
            # `if not q or not a: continue` below and did nothing at all.
            #
            # An entry with no answer is useless anyway: find_answer would
            # return an empty string and the robot would say nothing.
            if not a:
                target = None
                # Prefer the id map, since the question text may have been
                # edited since it was saved.
                if qid:
                    target = ids.pop(qid, None)
                # Fall back to the question text. Entries saved before the id
                # map existed have no mapping, so an id-only delete would find
                # nothing — which is the likely reason deletes appeared to be
                # ignored on a store that predates this code.
                if (not target or target not in qa) and q:
                    target = q if q in qa else None
                if target and target in qa:
                    del qa[target]
                    deleted += 1
                    print(f"[QA] deleted {target!r}")
                else:
                    print(f"[QA] nothing to delete for id={qid!r} q={q!r} "
                          f"— not in the store")
                continue

            if not q:
                continue

            # An id whose question has been EDITED: drop the old question, or
            # the store keeps both the old and new wording and the robot
            # answers whichever it matches first.
            if qid:
                prev = ids.get(qid)
                if prev and prev != q and prev in qa:
                    del qa[prev]
                ids[qid] = q

            qa[q] = a
            added += 1

        save_qa(qa)
        _save_qa_ids(ids)
        print(f"[QA] {added} added or updated, {deleted} deleted, "
              f"{len(qa)} total")
        return jsonify({"status": "ok", "added": added,
                        "deleted": deleted}), 200
    except Exception as e:
        print(f"[QA] Error: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/qa/list', methods=['GET'])
def list_qa():
    try:
        return jsonify(load_qa())
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/qa/delete', methods=['POST'])
def delete():
    try:
        q = request.json.get('question','').lower().strip()
        qa = load_qa()
        if q in qa:
            del qa[q]
            save_qa(qa)
            return jsonify({"status": "ok", "message": f"Deleted: {q}"})
        return jsonify({"status": "error", "message": "Question not found"}), 404
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/qa/update', methods=['POST'])
def update_qa():
    try:
        data         = request.json
        old_question = data.get('old_question','').strip().lower()
        new_question = data.get('new_question','').strip().lower()
        new_answer   = data.get('new_answer','').strip()
        if not old_question or not new_question or not new_answer:
            return jsonify({"status": "error", "message": "old_question, new_question and new_answer required"}), 400
        qa = load_qa()
        if old_question in qa:
            del qa[old_question]
        qa[new_question] = new_answer
        save_qa(qa)
        print(f"[QA] Updated: '{old_question}' -> '{new_question}'")
        return jsonify({"status": "ok", "message": "Q&A updated"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/qa/test-voice', methods=['POST'])
def test_voice():
    from audio_utils import speak
    s = load_settings()
    lang = s.get('language', 'en')
    if lang == 'ta':
        threading.Thread(target=speak, args=("வணக்கம், நான் உங்கள் உதவியாளர்",)).start()
    else:
        threading.Thread(target=speak, args=("Hello, I am your Pi assistant",)).start()
    return jsonify({"status": "ok"})

# ── Face ───────────────────────────────────────────────────
@app.route('/face/add', methods=['POST'])
def face_add():
    try:
        name     = request.form.get('name', '').strip()
        greeting = request.form.get('greeting', '').strip()
        file     = request.files.get('image')
        if not name or not greeting or not file:
            return jsonify({"status": "error", "message": "name, greeting and image required"}), 400
        filename = secure_filename(f"{name}.jpg")
        path     = os.path.join(UPLOAD_FOLDER, filename)
        file.save(path)
        success = register_face(name, greeting, path)
        if success:
            return jsonify({"status": "ok", "message": f"{name} registered"})
        return jsonify({"status": "error", "message": "No face found in image"}), 400
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/face/list', methods=['GET'])
def face_list():
    try:
        return jsonify(list_faces())
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/face/delete', methods=['POST'])
def face_delete():
    try:
        name = request.json.get('name', '').strip()
        if delete_face(name):
            return jsonify({"status": "ok", "message": f"Deleted {name}"})
        return jsonify({"status": "error", "message": "Not found"}), 404
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/upload_faces', methods=['POST'])
def handle_upload_faces():
    """Faces from the NEW app: a JSON list, images as base64 data URLs.

        [{"id": "1", "face": "data:image/jpeg;base64,...", "speech": "Hi Ravi"}]

    A completely different shape from /upload-face below, which expects a
    multipart form with an actual file. The two apps disagree, so both are
    supported rather than one being broken.

    An entry with face and speech BOTH empty means delete that face — the same
    convention the new app uses for Q&A.
    """
    try:
        import base64
        data = request.get_json(silent=True)
        if not isinstance(data, list):
            return jsonify({"status": "error",
                            "message": "expected a JSON list"}), 400

        saved = deleted = failed = 0
        for item in data:
            if not isinstance(item, dict):
                continue
            fid = str(item.get('id', '')).strip()
            b64 = item.get('face', '') or ''
            speech = str(item.get('speech', '')).strip()
            if not fid:
                continue

            face_name = f"Face_{fid}"

            if not b64 and not speech:
                try:
                    delete_face(face_name)
                    deleted += 1
                except Exception as e:
                    print(f"[FACE] could not delete {face_name}: {e}")
                continue

            if not b64:
                # Greeting changed but no new photo. Re-registering without an
                # image would fail, so keep the existing embedding and only
                # update the words — if the store supports it.
                print(f"[FACE] {face_name}: speech only, no new image")
                continue

            try:
                if ',' in b64:
                    b64 = b64.split(',', 1)[1]
                img = base64.b64decode(b64)
                path = os.path.join(UPLOAD_FOLDER,
                                    secure_filename(f"{face_name}.jpg"))
                with open(path, 'wb') as f:
                    f.write(img)
                # register_face runs the detector: it returns False when there
                # is no face in the picture, which is worth reporting rather
                # than counting as success. A silently unregistered face looks
                # like recognition being broken later.
                if register_face(face_name, speech or face_name, path):
                    saved += 1
                    print(f"[FACE] registered {face_name}")
                else:
                    failed += 1
                    print(f"[FACE] {face_name}: no face found in the image")
            except Exception as e:
                failed += 1
                print(f"[FACE] {face_name}: {e}")

        return jsonify({"status": "ok", "saved": saved,
                        "deleted": deleted, "failed": failed}), 200
    except Exception as e:
        print(f"[FACE] Error: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/upload-face', methods=['POST'])
def handle_upload_face():
    try:
        face_index = request.form.get('faceIndex', '0')
        speech     = request.form.get('speech', '').strip()
        file       = request.files.get('image')
        if not file:
            return jsonify({"status": "error", "message": "No image file provided"}), 400
        if not speech:
            return jsonify({"status": "error", "message": "No greeting text provided"}), 400
        allowed = {'jpg', 'jpeg', 'png'}
        if not any(file.filename.lower().endswith(f'.{ext}') for ext in allowed):
            return jsonify({"status": "error", "message": "Only JPG, JPEG, PNG supported"}), 400
        face_name = f"Face_{face_index}"
        path = os.path.join(UPLOAD_FOLDER, secure_filename(f"{face_name}.jpg"))
        file.save(path)
        success = register_face(face_name, speech, path)
        if success:
            print(f"[FACE] Registered Face {face_index}")
            return jsonify({"status": "ok", "message": f"Face {face_index} registered"}), 200
        return jsonify({"status": "error", "message": "No face detected in image"}), 400
    except Exception as e:
        print(f"[FACE] Error: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

# ── Settings ───────────────────────────────────────────────
@app.route('/settings', methods=['GET'])
def get_settings():
    """Return the stored settings — and apply any setting passed as a query
    parameter on the way through.

    The app drives the volume slider with a GET to this route, with the value
    in the query string. This route used to ignore request.args entirely, so
    every slider movement got a 200 and changed nothing. See apply_volume().
    """
    vol = request.args.get('volume')
    if vol is not None:
        try:
            save_settings({"volume": max(0, min(100, int(float(vol))))})
        except (TypeError, ValueError):
            print(f"[VOLUME] bad volume parameter {vol!r}")
        apply_volume(vol)
    return jsonify(load_settings())

@app.route('/settings', methods=['POST'])
def update_settings():
    try:
        data = request.json
        s = save_settings(data)
        # Storing the value is not enough on its own: nothing else in the
        # codebase touches the mixer, so without this the slider would still
        # be decorative.
        if isinstance(data, dict) and 'volume' in data:
            apply_volume(data['volume'])
        return jsonify({"status": "ok", "settings": s})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/settings/apikey', methods=['POST'])
def set_apikey():
    try:
        key = request.json.get('api_key', '').strip()
        if not key:
            return jsonify({"status": "error", "message": "Empty key"}), 400
        with open("ai_config.json", "w") as f:
            json.dump({"openai_key": key}, f)
        return jsonify({"status": "ok", "message": "API key saved"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

# ── Save Audio ─────────────────────────────────────────────
@app.route('/save-audio', methods=['GET', 'POST'])
def save_audio():
    """Robot name and greeting, and optionally a full Q&A replacement.

    ── DATA LOSS FIXED HERE ────────────────────────────────────────────
    This used to do, unconditionally:

        qa_list = data.get('qa', [])
        ...build new_qa from it...
        save_qa(new_qa)

    The NEW app does not send a 'qa' field — it saves Q&A separately through
    /qa/add. So qa_list came back empty, new_qa was {}, and save_qa({}) WIPED
    EVERY SAVED Q&A. Silently: the endpoint returned 200 and logged "Replaced
    with 0 Q&As". Changing the robot's name would have erased its entire
    knowledge base.

    Now the Q&A store is only touched when a 'qa' field is actually PRESENT.
    An absent field means "not my business", which is different from an empty
    list meaning "delete everything" — and only the old app ever meant the
    latter.

    Also accepts GET, because the new app uses query parameters.
    """
    try:
        if request.method == 'POST':
            data = request.get_json(silent=True) or {}
            if not data:
                data = request.args.to_dict()
        else:
            data = request.args.to_dict()

        robot_name     = str(data.get('robotName', '')).strip()
        welcome_speech = str(data.get('welcomeSpeech', '')).strip()

        updates = {}
        if robot_name:
            updates["robot_name"] = robot_name
        if welcome_speech:
            updates["welcome_speech"] = welcome_speech
        if updates:
            save_settings(updates)

        # ONLY when the field is present. See the note above.
        if 'qa' in data and isinstance(data.get('qa'), list):
            new_qa = {}
            for item in data['qa']:
                q = str(item.get('question', item.get('q', ''))).strip().lower()
                a = str(item.get('answer',   item.get('a', ''))).strip()
                if q and a:
                    new_qa[q] = a
            # FLAT {question: answer}, matching what find_answer() reads.
            save_qa(new_qa)
            # The id map has to go with it, or deletes afterwards would look
            # up ids pointing at questions that no longer exist.
            _save_qa_ids({})
            print(f"[AUDIO] replaced the Q&A store with {len(new_qa)} entries")
        else:
            print("[AUDIO] no 'qa' field — Q&A store left alone")

        print(f"[AUDIO] name={robot_name!r} welcome={welcome_speech!r}")
        return jsonify({"status": "ok"}), 200
    except Exception as e:
        print(f"[AUDIO] Error: {e}")
        return jsonify({"error": str(e)}), 400

# ── Volume (explicit route) ────────────────────────────────
@app.route('/volume', methods=['GET', 'POST'])
def handle_volume():
    """A route of its own, in case the app is ever pointed at one.

    Deliberately NOT behind @require_control_session: volume is not a motion
    command, and locking it would mean the robot could not be quietened
    without first acquiring control.
    """
    body = request.get_json(silent=True) or {}
    value = (request.args.get('value') or request.args.get('volume')
             or body.get('value') or body.get('volume'))
    if value is None:
        return jsonify({"status": "ok",
                        "volume": load_settings().get('volume')}), 200
    try:
        value = max(0, min(100, int(float(value))))
    except (TypeError, ValueError):
        return jsonify({"status": "error",
                        "message": "volume must be a number"}), 400
    save_settings({"volume": value})
    ok = apply_volume(value)
    return jsonify({"status": "ok", "volume": value, "applied": ok}), 200

# ── Hardware Toggle ────────────────────────────────────────
@app.route('/hardware', methods=['GET'])
@app.route('/obstacle_avoidance', methods=['GET'])
@require_control_session
def handle_hardware():
    """Obstacle avoidance on or off.

    TWO paths and TWO parameter names, because the apps disagree:

        old app : /hardware?state=on|off
        new app : /obstacle_avoidance?value=up|down

    The new app was getting a 404 on every toggle — the switch moved in the
    UI and nothing reached the robot. "up" means enabled, matching the
    slider-style control the app uses.
    """
    global hardware_enabled
    raw = (request.args.get('state')
           or request.args.get('value')
           or 'on').strip().lower()
    hardware_enabled = raw in ('on', 'up', 'true', 'yes', '1', 'enabled')
    print(f"[HARDWARE] {'Enabled' if hardware_enabled else 'Disabled'} "
          f"(from {raw!r})")
    send_to_esp32(f"HARDWARE:{'ON' if hardware_enabled else 'OFF'}")
    return jsonify({"status": "ok", "hardware": hardware_enabled,
                    "obstacle_avoidance": hardware_enabled}), 200


# ══════════════════════════════════════════════════════════════════════
# MOVEMENT KEEPALIVE
#
# One /move per button press is not enough to hold the robot moving. Two
# possibilities, and the keepalive is right for both:
#
#   IF THE FIRMWARE HAS A WATCHDOG — Ben's and Luna's do:
#       if (millis() - lastCmdTime > 500 && robotMoving) stopMotors();
#   then the motors stop half a second after the last command, however long
#   the button is held. That watchdog should STAY: it is the only thing that
#   halts the robot if this Pi dies mid-drive. What it needs is feeding.
#
#   IF THE FIRMWARE HAS NO WATCHDOG, one MOVE:forward makes the robot drive
#   until something else stops it — so a lost release event, a closed app or a
#   dropped WiFi connection means it keeps going. Then the hold timeout below
#   is the only stop that exists, which makes it more important, not less.
#
# Check which you have:  grep -n lastCmdTime <your>.ino
#
# ─── THE TRADE-OFF, MEASURED ON LUNA ────────────────────────────────────
#   HOLD = 8s   app sends once, held 5s ......  5.0s  correct
#               app dies mid-press ...........  8.0s  runaway
#               client repeats 1s, dies ...... 11.0s  runaway, and WORSE
#   HOLD = 2s   app sends once, held 5s ......  2.0s  CUT OFF after 2s
#               client repeats 1s, held 5s ...  5.0s  correct
#
# A client that repeats makes a crash last LONGER at a long timeout, because
# every repeat pushes the deadline out again. So a short timeout only works
# once EVERY client repeats.
#
# Keep 8.0 while the app sends one request per press. Drop to 2.0 only after
# the app repeats every second — otherwise the D-pad dies after two seconds
# and it looks like this change broke it.
# ══════════════════════════════════════════════════════════════════════
MOVE_KEEPALIVE_SEC = 0.3      # must be comfortably under the firmware's 0.5s
MOVE_HOLD_TIMEOUT  = 8.0      # see above before changing

_move_dir      = "stop"
_move_deadline = 0.0
_move_lock     = threading.Lock()


def stop_movement():
    """Clear the keepalive and stop. Used by shutdown and restart.

    Clearing the direction BEFORE sending the stop matters: otherwise the
    keepalive thread resends the old direction 300ms later and the robot
    carries on after being told to stop.
    """
    global _move_dir
    with _move_lock:
        _move_dir = "stop"
    send_to_esp32("MOVE:stop")


def _move_keepalive():
    global _move_dir
    while True:
        time.sleep(MOVE_KEEPALIVE_SEC)
        with _move_lock:
            d, deadline = _move_dir, _move_deadline
        if d == "stop":
            continue                       # idle: no serial traffic at all
        if time.time() > deadline:
            with _move_lock:
                _move_dir = "stop"
            print(f"[MOVE] no refresh for {MOVE_HOLD_TIMEOUT:.0f}s — stopping")
            send_to_esp32("MOVE:stop")
            continue
        send_to_esp32(f"MOVE:{d}", quiet=True)


# ── Movement ───────────────────────────────────────────────
@app.route('/move', methods=['GET'])
@require_control_session
def handle_move():
    global _move_dir, _move_deadline
    direction = request.args.get('dir', 'stop')
    # The app says 'back'; the firmware expects 'backward'. Harmless if this
    # firmware accepts either, and necessary if it does not.
    if direction == 'back':
        direction = 'backward'

    with _move_lock:
        changed = (direction != _move_dir)
        _move_dir = direction
        _move_deadline = time.time() + MOVE_HOLD_TIMEOUT

    # Sent at once, so the robot reacts on the press rather than on the next
    # keepalive tick up to 300ms later.
    send_to_esp32(f"MOVE:{direction}", quiet=not changed)
    if changed:
        print(f"[MOVE] {direction}")
    return "OK", 200

@app.route('/speed', methods=['GET'])
@require_control_session
def handle_speed():
    value = request.args.get('value', '50')
    print(f"[SIGNAL] SPEED: {value}")
    send_to_esp32(f"SPEED:{value}")
    return "OK", 200

@app.route('/topspeed', methods=['GET'])
@require_control_session
def handle_topspeed():
    value = request.args.get('value', '50')
    print(f"[SIGNAL] TOPSPEED: {value}")
    send_to_esp32(f"TOPSPEED:{value}")
    return "OK", 200

# ── Position ───────────────────────────────────────────────
@app.route('/position', methods=['GET'])
@require_control_session
def handle_position():
    part  = request.args.get('part', '')
    value = request.args.get('value', '1000')
    hand  = request.args.get('hand', 'left')
    print(f"[SIGNAL] POSITION: {part} -> {value} ({hand})")
    send_to_esp32(f"POS:{part}:{value}:{hand}")
    return "OK", 200

@app.route('/hand', methods=['GET'])
@require_control_session
def handle_hand():
    value = request.args.get('value', 'left')
    print(f"[SIGNAL] HAND: {value}")
    send_to_esp32(f"HAND:{value}")
    return "OK", 200

@app.route('/home', methods=['GET'])
@require_control_session
def handle_home():
    print("[SIGNAL] HOME")
    send_to_esp32("HOME")
    return "OK", 200

@app.route('/save_pose', methods=['GET'])
@require_control_session
def handle_save_pose():
    pos  = request.args.get('pos', '')
    hand = request.args.get('hand', 'left')
    parts = ['headLR','headUD','lateral','shoulder','forearm','elbow','wrist','fingers']
    pose_data = {'hand': hand, 'servos': {}}
    for part in parts:
        val = request.args.get(part)
        if val is not None:
            pose_data['servos'][part] = int(val)
    poses = load_poses()
    poses[str(pos)] = pose_data
    save_poses(poses)
    print(f"[POSE] Saved pose {pos}: {pose_data}")
    send_to_esp32(f"SAVE_POSE:{pos}")
    return "OK", 200

# ── Eyes ───────────────────────────────────────────────────
@app.route('/eyes1', methods=['GET'])
@app.route('/eyes2', methods=['GET'])
@app.route('/eyes3', methods=['GET'])
@require_control_session
def handle_eyes():
    eye = request.path[-1]
    print(f"[SIGNAL] EYES: {eye}")
    send_to_esp32(f"EYES:{eye}")
    try:
        from eyes import set_state
        set_state(f"eyes{eye}")
    except Exception as e:
        print(f"[EYES] Error: {e}")
    return "OK", 200

@app.route('/mode1', methods=['GET'])
@app.route('/mode2', methods=['GET'])
@app.route('/mode3', methods=['GET'])
@require_control_session
def handle_mode():
    mode  = request.path[-1]
    value = request.args.get('value', 'A')
    print(f"[SIGNAL] MODE {mode}: {value}")
    send_to_esp32(f"MODE:{mode}:{value}")
    return "OK", 200

# ── Loop ───────────────────────────────────────────────────
@app.route('/loop_start', methods=['GET'])
@require_control_session
def handle_loop_start():
    global loop_running, loop_thread
    print("[SIGNAL] LOOP: START")
    loop_running = True
    loop_thread  = threading.Thread(target=run_loop, daemon=True)
    loop_thread.start()
    send_to_esp32("LOOP:START")
    return "OK", 200

@app.route('/loop_stop', methods=['GET'])
@require_control_session
def handle_loop_stop():
    global loop_running
    print("[SIGNAL] LOOP: STOP")
    loop_running = False
    send_to_esp32("LOOP:STOP")
    return "OK", 200

@app.route('/loop_undo', methods=['GET'])
@require_control_session
def handle_loop_undo():
    print("[SIGNAL] LOOP: UNDO")
    poses = load_poses()
    if poses:
        last_key = str(max(poses.keys(), key=int))
        del poses[last_key]
        save_poses(poses)
        print(f"[LOOP] Deleted pose {last_key}")
    send_to_esp32("LOOP:UNDO")
    return "OK", 200

@app.route('/loop_delete', methods=['GET'])
@require_control_session
def handle_loop_delete():
    print("[SIGNAL] LOOP: DELETE ALL")
    save_poses({})
    send_to_esp32("LOOP:DELETE")
    return "OK", 200

@app.route('/select_model', methods=['GET'])
@require_control_session
def handle_select_model():
    name = request.args.get('name', 'None')
    print(f"[SIGNAL] MODEL: {name}")
    send_to_esp32(f"MODEL:{name}")
    return "OK", 200

# ── Shutdown ───────────────────────────────────────────────
@app.route('/shutdown', methods=['GET'])
@require_control_session
def handle_shutdown():
    threading.Thread(target=do_shutdown, daemon=True).start()
    return jsonify({"status": "ok", "message": "Shutting down..."}), 200

@app.route('/restart', methods=['GET'])
@require_control_session
def handle_restart():
    def do_restart():
        try:
            from eyes import set_state
            set_state("obstacle")
        except: pass
        stop_movement()
        send_to_esp32("HOME")
        time.sleep(5)
        # Call reboot DIRECTLY.
        #
        # The Popen("sleep 2 && reboot") + os._exit(0) pattern that was here
        # does not work under systemd: os._exit kills this process, systemd
        # tears down the whole cgroup, and the scheduled child dies before it
        # runs. Confirmed on this robot's journal — "Deactivated successfully"
        # followed by "Scheduled restart job" — so the service simply came back
        # and the Pi never rebooted.
        subprocess.run(["sudo", "/sbin/reboot"])
    threading.Thread(target=do_restart, daemon=True).start()
    return jsonify({"status": "ok", "message": "Restarting..."}), 200

@app.errorhandler(404)
def _log_unknown_route(e):
    """Log every request to a path that does not exist.

    Worth having permanently. On Luna, four features were silently broken
    because the app called an endpoint the server had never implemented —
    /fingers, /hardware, /upload-face and /topspeed all returned 404 with
    nothing in the log, and each was found only when somebody noticed a
    control did nothing. This turns that class of problem into one grep.
    """
    q = ("?" + request.query_string.decode()) if request.query_string else ""
    print(f"[404] {request.method} {request.path}{q}  from {request.remote_addr}"
          f"  <- the app wants an endpoint that does not exist")
    return jsonify({"status": "error", "message": "unknown endpoint",
                    "path": request.path}), 404


# ── Start ──────────────────────────────────────────────────
def start_server():
    threading.Thread(target=_move_keepalive, daemon=True).start()
    print(f"[MOVE] keepalive {MOVE_KEEPALIVE_SEC}s, "
          f"hold timeout {MOVE_HOLD_TIMEOUT}s")

    # Re-apply the saved volume at boot. ALSA mixer levels are not persisted
    # by this robot, so without this the slider's effect lasts only until the
    # next restart and the customer has to set it again every time.
    try:
        apply_volume(load_settings().get('volume', 80))
    except Exception as e:
        print(f"[VOLUME] could not apply saved volume at boot: {e}")

    try:
        # threaded=True matters: without it Flask serves one request at a
        # time, so a slow /position or /status call can delay the button
        # RELEASE event — and the robot keeps driving until the hold timeout.
        app.run(host='0.0.0.0', port=5000, debug=False, threaded=True)
    except OSError as e:
        # Do not carry on half-dead. Letting this bubble out of the thread
        # means the robot prints "Server started on port 5000" while serving
        # nothing, and the app is simply unreachable with one line in the log.
        print(f"[HTTP] *** CANNOT BIND PORT 5000: {e} ***")
        print("[HTTP] The app will NOT work. Find the process:")
        print("[HTTP]     sudo lsof -i :5000")
        os._exit(1)          # let systemd restart us rather than run broken

if __name__ == '__main__':
    start_server()
