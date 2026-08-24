import time, os, threading, struct, subprocess
from server import start_server, send_to_esp32
from audio_utils import record_audio, speak
from qa_store import find_answer, save_qa
from face_utils import scan_face_from_camera

# ── Shutdown flag ──────────────────────────────────────────
shutdown_in_progress = False

# ── Joystick config ────────────────────────────────────────
JOYSTICK_DEV = "/dev/input/js0"
DEADZONE     = 5000
EVENT_SIZE   = 8
EVENT_FMT    = "IhBB"

joy_axis     = [0] * 8
joy_buttons  = [0] * 15
joy_dir      = "stop"
joy_speed    = 70

# ── Factory defaults ───────────────────────────────────────
FACTORY_ROBOT_NAME = "Giya"
FACTORY_WIFI_SSID  = "GiyaRobot"
FACTORY_WIFI_PASS  = "giya1234"
FACTORY_IP         = "192.168.4.1"

# ── Wake word variants ─────────────────────────────────────
# NOTE: "yeah" and "ga" were in this list and are removed. Both appear in
# ordinary speech constantly, so Giya would drop into Q&A mode whenever anyone
# nearby said "yeah" — and because matching is a plain substring test, "ga"
# also fires on "again", "garden", "regard" and hundreds of others.
WAKE_WORDS = [
    "giya", "geya", "gia", "gya", "gea", "giyah", "guia",
    "jiya", "jia", "jeya",
    "hey giya", "hi giya", "ok giya", "okay giya",
    "giya please", "please giya", "dear giya","yeah",
]

# Word-boundary matching. The old version used `w in text`, so a single-syllable
# variant like "gia" matched inside "Nigeria" or "logical". Requiring whole
# words removes a whole class of false triggers at no cost.
import re
_WAKE_RE = re.compile(
    r'\b(' + '|'.join(re.escape(w) for w in sorted(WAKE_WORDS, key=len,
                                                   reverse=True)) + r')\b')


def is_wake_word(text: str) -> bool:
    return bool(_WAKE_RE.search((text or "").lower().strip()))


# Whisper's stock inventions on silence. Without this, "you" or "thank you"
# from an empty room reaches the "hi" branch below and Giya announces
# "Please look at the camera" to nobody.
_NOISE = {"", ".", "you", "thank you", "thanks", "okay", "ok", "oh", "hmm",
          "uh", "um", "bye", "hello", "hi", "yeah", "so", "the",
          "thanks for watching", "subs by", "subscribe"}


def is_noise(text: str) -> bool:
    t = (text or "").strip().lower().rstrip(".!?,")
    return t in _NOISE


# ── Eye helper ─────────────────────────────────────────────
def set_eye(state):
    try:
        from eyes import set_state
        set_state(state)
    except: pass


# ══════════════════════════════════════════════════════════════════════
# SPEECH TO TEXT
#
# The model is loaded ONCE, at first use, and reused.
#
# The previous version forked a fresh process and reloaded Whisper for every
# single utterance. On a Pi 5 that load is the 15-second gap that showed up in
# the log between "[MIC] Saved" and the next "[MIC] Recording":
#
#     11:32:53  Recording 3s
#     11:32:56  Saved
#     11:33:11  Heard: ''        <- 15 seconds later
#     11:33:11  Recording 3s
#
# So Giya was listening for 3 seconds out of every 18 — about 17% of the time.
# Anything said in the other 15 seconds was never recorded at all. The mic, the
# gain and the model were all fine; the robot simply was not listening when
# people spoke.
#
# The fork was presumably there to isolate a ctranslate2 crash. That is a real
# risk, but paying 15 seconds per utterance to avoid it makes the robot
# unusable — and a crash would now be visible in the log rather than silent.
# ══════════════════════════════════════════════════════════════════════

WHISPER_MODEL = "base.en"      # "tiny" also works and is faster, less accurate
_whisper = None
_whisper_lock = threading.Lock()


def _get_whisper():
    global _whisper
    with _whisper_lock:
        if _whisper is None:
            from faster_whisper import WhisperModel
            print(f"[STT] loading {WHISPER_MODEL}...")
            t0 = time.time()
            _whisper = WhisperModel(WHISPER_MODEL, device="cpu",
                                    compute_type="int8")
            print(f"[STT] ready in {time.time()-t0:.1f}s")
        return _whisper


def transcribe(wav_path: str) -> str:
    if not wav_path or not os.path.exists(wav_path):
        return ""
    if shutdown_in_progress:
        try: os.remove(wav_path)
        except: pass
        return ""
    try:
        t0 = time.time()
        segments, _ = _get_whisper().transcribe(
            wav_path,
            language="en",
            beam_size=1,                       # greedy: faster, ample here
            vad_filter=True,                   # skip silence: quicker, and
            condition_on_previous_text=False,  # stops repetition loops
        )
        text = " ".join(s.text for s in segments).lower().strip()
        for ch in ".,!?":
            text = text.replace(ch, "")
        text = " ".join(text.split())
        print(f"[STT] {time.time()-t0:.1f}s -> {text!r}")
        return text
    except Exception as e:
        # Say WHY nothing came back. Returning "" for both "heard silence" and
        # "the model failed" is what made this so slow to diagnose.
        import traceback
        print(f"[STT] error: {e}")
        traceback.print_exc()
        return ""
    finally:
        try: os.remove(wav_path)
        except: pass


# ── Q&A mode ───────────────────────────────────────────────
def qa_mode():
    from ai_fallback import ask_gpt
    print("[MODE] Q&A mode activated")
    set_eye("wake")
    speak("Yes?")
    set_eye("listening")
    wav_q = record_audio(duration=5)
    question = transcribe(wav_q)
    print(f"[QUESTION] {question!r}")
    if not question or is_noise(question):
        set_eye("speaking")
        speak("I did not catch that.")
        set_eye("idle")
        return
    answer = find_answer(question)
    if answer:
        print("[QA] Found in local store")
        set_eye("speaking")
        speak(answer)
    else:
        print("[QA] Not found locally - asking GPT...")
        set_eye("thinking")
        speak("Let me think about that.")
        answer = ask_gpt(question, language="en")
        set_eye("speaking")
        speak(answer)
    set_eye("idle")


# ── Face mode ──────────────────────────────────────────────
def face_mode():
    print("[MODE] Face detection mode activated")
    set_eye("face")
    speak("Please look at the camera")
    try:
        name, greeting = scan_face_from_camera(timeout=7)
        if name:
            set_eye("speaking")
            speak(f"Hello {name}, {greeting}")
        else:
            set_eye("speaking")
            speak("Sorry, I do not recognize you")
    except Exception as e:
        print(f"[FACE] Error: {e}")
    set_eye("idle")


# ── Shutdown ───────────────────────────────────────────────
def do_shutdown():
    global shutdown_in_progress
    shutdown_in_progress = True
    print("[SHUTDOWN] Starting safe shutdown")
    time.sleep(0.5)
    try:
        set_eye("obstacle")
    except: pass
    try:
        speak("Shutting down. Goodbye!")
    except Exception as e:
        print(f"[SHUTDOWN] Speak error: {e}")
    try:
        send_to_esp32("MOVE:stop")
        time.sleep(1)
    except Exception as e:
        print(f"[SHUTDOWN] Stop error: {e}")
    try:
        send_to_esp32("HOME")
        time.sleep(2)
    except Exception as e:
        print(f"[SHUTDOWN] Home error: {e}")
    try:
        send_to_esp32("LATCH:OFF")
        print("[SHUTDOWN] LATCH:OFF sent - ESP32 cuts power in 15s")
        time.sleep(1)
    except Exception as e:
        print(f"[SHUTDOWN] Latch error: {e}")

    print("[SHUTDOWN] Executing poweroff")
    # Call shutdown DIRECTLY.
    #
    # The previous version did:
    #     subprocess.Popen(["bash","-c","sleep 2 && sudo /sbin/shutdown -h now"])
    #     os._exit(0)
    # That does not work under systemd. os._exit kills this process, systemd
    # then tears down the whole cgroup by default, and the "sleep 2" child dies
    # before it ever runs. The journal showed exactly that: "Deactivated
    # successfully" followed by "Scheduled restart job"  the service simply
    # came back, while the ESP32 cut power 15 seconds later on a machine that
    # had never shut down. Every button press was a hard power cut.
    #
    # `shutdown -h now` hands off to systemd and returns immediately, so this
    # does not block the caller either.
    subprocess.run(["sudo", "/sbin/shutdown", "-h", "now"])


# ── Factory Reset ──────────────────────────────────────────
def do_factory_reset():
    print("[FACTORY] Resetting to factory defaults...")
    set_eye("thinking")
    try:
        speak("Resetting to factory settings. Please wait.")
    except: pass

    from settings import save_settings
    save_settings({
        "robot_name":      FACTORY_ROBOT_NAME,
        "welcome_speech":  f"Hello, I am {FACTORY_ROBOT_NAME}, your robot assistant",
        "language":        "en",
        "voice":           "female",
        "chatgpt_enabled": True
    })
    print("[FACTORY] settings.json reset")

    save_qa({})
    print("[FACTORY] qa_store.json cleared")

    try:
        result = subprocess.run(
            ['sudo', 'nmcli', '-t', '-f', 'NAME,TYPE', 'connection', 'show'],
            capture_output=True, text=True
        )
        for line in result.stdout.splitlines():
            if ':wifi' in line:
                conn_name = line.split(':')[0]
                print(f"[FACTORY] Deleting wifi connection: {conn_name}")
                subprocess.run(
                    ['sudo', 'nmcli', 'connection', 'delete', conn_name],
                    capture_output=True
                )
        subprocess.run([
            'sudo', 'nmcli', 'connection', 'add',
            'type', 'wifi',
            'ifname', 'wlan0',
            'con-name', FACTORY_WIFI_SSID,
            'autoconnect', 'yes',
            'ssid', FACTORY_WIFI_SSID,
            '802-11-wireless.mode', 'ap',
            '802-11-wireless.band', 'bg',
            'ipv4.method', 'shared',
            'ipv4.addresses', f'{FACTORY_IP}/24',
            'wifi-sec.key-mgmt', 'wpa-psk',
            'wifi-sec.psk', FACTORY_WIFI_PASS
        ], capture_output=True)
        subprocess.run([
            'sudo', 'nmcli', 'connection', 'modify',
            FACTORY_WIFI_SSID,
            'connection.autoconnect-priority', '100'
        ], capture_output=True)
        subprocess.run(
            ['sudo', 'nmcli', 'connection', 'up', FACTORY_WIFI_SSID],
            capture_output=True
        )
        print(f"[FACTORY] WiFi -> '{FACTORY_WIFI_SSID}' / '{FACTORY_WIFI_PASS}' @ {FACTORY_IP}")
    except Exception as e:
        print(f"[FACTORY] WiFi reset error: {e}")

    set_eye("idle")
    try:
        speak(f"Factory reset complete. Connect to WiFi {FACTORY_WIFI_SSID} with password {FACTORY_WIFI_PASS}.")
    except: pass

    print("[FACTORY] Done - restarting service")
    time.sleep(3)
    subprocess.run(['sudo', 'systemctl', 'restart', 'piassistant'])


# ── Joystick ───────────────────────────────────────────────
def get_direction():
    y = joy_axis[1]
    x = joy_axis[2]
    if abs(y) >= DEADZONE and abs(y) >= abs(x):
        return "forward" if y < -DEADZONE else "backward"
    if abs(x) >= DEADZONE:
        return "left" if x < -DEADZONE else "right"
    return "stop"


def joystick_loop():
    global joy_dir, joy_speed
    last_servo_time = 0
    headLR_pos  = 1000
    latL_pos    = 1000
    latR_pos    = 1000

    def axis_to_servo(val):
        return int((val + 32767) / 65534 * 2000)

    def keepalive():
        while True:
            time.sleep(0.3)
            if joy_dir and joy_dir != "stop":
                send_to_esp32(f"MOVE:{joy_dir}")
    threading.Thread(target=keepalive, daemon=True).start()

    while True:
        try:
            js = open(JOYSTICK_DEV, "rb")
            print("[JOY] Joystick connected")
            send_to_esp32(f"SPEED:{joy_speed}")

            while True:
                event = js.read(EVENT_SIZE)
                if not event:
                    break
                t, value, etype, number = struct.unpack(EVENT_FMT, event)
                if etype & 0x80:
                    continue

                if etype == 2:
                    if number < len(joy_axis):
                        joy_axis[number] = value

                    if number in [1, 2]:
                        if not joy_buttons[14] and not joy_buttons[13] and not joy_buttons[1] and not joy_buttons[3]:
                            new_dir = get_direction()
                            if new_dir != joy_dir:
                                joy_dir = new_dir
                                send_to_esp32(f"MOVE:{joy_dir}")
                                print(f"[JOY] {joy_dir}")
                                set_eye(joy_dir if joy_dir != "stop" else "idle")

                    if time.time() - last_servo_time > 0.05:
                        if joy_buttons[14] and number == 3 and abs(value) > DEADZONE:
                            latR_pos = max(0, min(2000, axis_to_servo(value)))
                            send_to_esp32(f"POS:elbow:{latR_pos}:right")
                            last_servo_time = time.time()

                        elif joy_buttons[13] and number == 1 and abs(value) > DEADZONE:
                            latL_pos = max(0, min(2000, axis_to_servo(value)))
                            send_to_esp32(f"POS:elbow:{latL_pos}:left")
                            last_servo_time = time.time()

                        elif joy_buttons[1] and number == 3 and abs(value) > DEADZONE:
                            pos = max(0, min(2000, axis_to_servo(value)))
                            latL_pos = latR_pos = pos
                            send_to_esp32(f"POS:elbow:{pos}:both")
                            last_servo_time = time.time()

                        elif joy_buttons[3] and number == 1 and abs(value) > DEADZONE:
                            headLR_pos = max(0, min(2000, axis_to_servo(value)))
                            send_to_esp32(f"POS:headLR:{headLR_pos}:left")
                            last_servo_time = time.time()

                elif etype == 1:
                    if number < len(joy_buttons):
                        joy_buttons[number] = value

                    if value == 1:
                        if number == 0:
                            joy_dir = "stop"
                            send_to_esp32("MOVE:stop")
                            set_eye("idle")
                            print("[JOY] stop")

                        elif number == 7:
                            joy_speed = min(100, joy_speed + 10)
                            send_to_esp32(f"SPEED:{joy_speed}")
                            print(f"[JOY] Speed: {joy_speed}%")

                        elif number == 6:
                            joy_speed = max(10, joy_speed - 10)
                            send_to_esp32(f"SPEED:{joy_speed}")
                            print(f"[JOY] Speed: {joy_speed}%")

                        elif number == 8:
                            headLR_pos = latL_pos = latR_pos = 1000
                            send_to_esp32("HOME")
                            print("[JOY] Servos homed")

                        elif number == 9:
                            joy_dir = "stop"
                            headLR_pos = latL_pos = latR_pos = 1000
                            send_to_esp32("MOVE:stop")
                            send_to_esp32("HOME")
                            set_eye("idle")
                            print("[JOY] Full stop + home")

                    if value == 0 and number in [13, 14, 1, 2]:
                        joy_dir = "stop"
                        send_to_esp32("MOVE:stop")
                        set_eye("idle")

            js.close()
            print("[JOY] Joystick disconnected - retrying in 3s...")

        except FileNotFoundError:
            pass
        except Exception as e:
            print(f"[JOY] Error: {e}")
        time.sleep(3)


# ── Voice listening loop ───────────────────────────────────
def listening_loop():
    print("[MAIN] Giya ready. Listening for wake word...")
    set_eye("speaking")
    speak("Hello, I am Giya, your robot assistant.")
    set_eye("idle")

    # Load Whisper BEFORE the first recording. Otherwise the first utterance
    # of the session pays the model-load time on top of transcription, and the
    # very first thing anyone says to the robot is the one thing it misses.
    _get_whisper()

    while True:
        if shutdown_in_progress:
            print("[MAIN] Shutdown in progress - stopping listening loop")
            break

        wav = record_audio(duration=3)

        if shutdown_in_progress:
            print("[MAIN] Shutdown in progress - skipping transcribe")
            try: os.remove(wav)
            except: pass
            break

        if wav is None:
            # No mic available. Back off rather than spinning on it — without
            # this, a missing mic produces hundreds of log lines a second.
            time.sleep(2)
            continue

        text = transcribe(wav)

        if shutdown_in_progress:
            break

        if not text or is_noise(text):
            time.sleep(0.1)
            continue

        print(f"[STT] Heard: {text!r}")

        if is_wake_word(text):
            print(f"[MAIN] Wake word detected in: {text!r}")
            qa_mode()
        elif re.search(r'\b(hi|hello)\s+giya\b', text) or "who am i" in text:
            # Was: `"hi" in text.split() or text.startswith("hi")`. Whisper
            # emits a bare "Hi." on silence, so face mode fired at random and
            # Giya told an empty room to look at the camera.
            print("[MAIN] Face mode trigger!")
            face_mode()

        time.sleep(0.1)


# ── Entry point ────────────────────────────────────────────
if __name__ == "__main__":
    # multiprocessing.set_start_method('fork') removed with the worker.
    # Nothing forks now, and fork() after native libraries are loaded is
    # unpredictable anyway.

    server_thread = threading.Thread(target=start_server, daemon=True)
    server_thread.start()
    print("[HTTP] Server started on port 5000")
    time.sleep(1)

    joy_thread = threading.Thread(target=joystick_loop, daemon=True)
    joy_thread.start()
    print("[JOY] Joystick thread started")

    try:
        from eyes import start_eyes
        start_eyes()
        print("[EYES] Started")
    except Exception as e:
        print(f"[EYES] Not started: {e}")

    try:
        listening_loop()
    except KeyboardInterrupt:
        try:
            from eyes import stop_eyes
            stop_eyes()
        except: pass
        send_to_esp32("MOVE:stop")
        print("\n[MAIN] Stopped.")
