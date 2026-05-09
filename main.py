import time, os, threading, struct
from faster_whisper import WhisperModel
from server import start_server, send_to_esp32
from audio_utils import record_audio, speak
from qa_store import find_answer
from face_utils import scan_face_from_camera

model = WhisperModel("tiny", device="cpu", compute_type="int8")

# ── Joystick config ──────────────────────────────────
JOYSTICK_DEV = "/dev/input/js0"
DEADZONE     = 5000
EVENT_SIZE   = 8
EVENT_FMT    = "IhBB"

joy_axis     = [0] * 8
joy_dir      = "stop"
joy_speed    = 70

# ── Eye helper ───────────────────────────────────────
def set_eye(state):
    try:
        from eyes import set_state
        set_state(state)
    except: pass

# ── Transcribe ───────────────────────────────────────
def transcribe(wav_path: str) -> str:
    if not wav_path or not os.path.exists(wav_path):
        return ""
    try:
        segments, _ = model.transcribe(wav_path, language="en")
        os.remove(wav_path)
        text = " ".join([s.text for s in segments]).lower().strip()
        text = text.replace(".", "").replace(",", "").replace("!", "").replace("?", "")
        return text.strip()
    except Exception as e:
        print(f"[STT] Error: {e}")
        return ""

# ── Q&A mode ─────────────────────────────────────────
def qa_mode():
    from settings import load_settings
    from ai_fallback import ask_gpt
    print("[MODE] Q&A mode activated")
    set_eye("wake")
    speak("How can I help?")
    set_eye("listening")
    wav_q = record_audio(duration=5)
    question = transcribe(wav_q)
    print(f"[QUESTION] {question!r}")
    if not question:
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
        s = load_settings()
        lang = s.get("language", "en")
        answer = ask_gpt(question, language=lang)
        set_eye("speaking")
        speak(answer)
    set_eye("idle")

# ── Face mode ─────────────────────────────────────────
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

# ── Joystick ──────────────────────────────────────────
def get_direction():
    y  = joy_axis[1]   # forward / backward
    a2 = joy_axis[2]   # left
    a3 = joy_axis[3]   # right

    # forward / backward takes priority
    if abs(y) > DEADZONE:
        return "forward" if y < -DEADZONE else "backward"

    # left / right from axis 2 and 3
    if a2 < -DEADZONE or a3 > DEADZONE:
        return "left"
    if a2 > DEADZONE or a3 < -DEADZONE:
        return "right"

    return "stop"

def joystick_loop():
    global joy_dir, joy_speed
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

                # axis event
                if etype == 2 and number < len(joy_axis):
                    joy_axis[number] = value
                    new_dir = get_direction()
                    if new_dir != joy_dir:
                        joy_dir = new_dir
                        send_to_esp32(f"MOVE:{joy_dir}")
                        print(f"[JOY] {joy_dir}")

                # button pressed
                elif etype == 1 and value == 1:
                    if number == 0:    # BtnA — stop
                        joy_dir = "stop"
                        send_to_esp32("MOVE:stop")
                        print("[JOY] stop")
                    elif number == 7:  # BtnTR — speed up
                        joy_speed = min(100, joy_speed + 10)
                        send_to_esp32(f"SPEED:{joy_speed}")
                        print(f"[JOY] Speed: {joy_speed}%")
                    elif number == 6:  # BtnTL — speed down
                        joy_speed = max(10, joy_speed - 10)
                        send_to_esp32(f"SPEED:{joy_speed}")
                        print(f"[JOY] Speed: {joy_speed}%")

            js.close()
            print("[JOY] Joystick disconnected — retrying in 3s...")

        except FileNotFoundError:
            pass
        except Exception as e:
            print(f"[JOY] Error: {e}")

        time.sleep(3)

# ── Voice listening loop ──────────────────────────────
def listening_loop():
    from settings import load_settings
    s          = load_settings()
    welcome    = s.get("welcome_speech", "System ready")
    robot_name = s.get("robot_name", "Pi Assistant")
    wake_word  = robot_name.lower().strip()
    print(f"[MAIN] {robot_name} ready. Wake word: {wake_word!r}")
    set_eye("speaking")
    speak(welcome if welcome else "System ready")
    set_eye("idle")
    while True:
        s          = load_settings()
        robot_name = s.get("robot_name", "Pi Assistant")
        wake_word  = robot_name.lower().strip()
        wav  = record_audio(duration=3)
        text = transcribe(wav)
        print(f"[STT] Heard: {text!r}")
        if wake_word and wake_word in text:
            print(f"[MAIN] Wake word {wake_word!r} detected!")
            qa_mode()
        elif "hi" in text.split() or text.startswith("hi"):
            print("[MAIN] Face mode trigger!")
            face_mode()
        time.sleep(0.1)

# ── Entry point ───────────────────────────────────────
if __name__ == "__main__":
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
