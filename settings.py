import json, os, threading

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
VOICES_DIR = os.path.join(BASE_DIR, "voices")
SETTINGS_FILE = os.path.join(BASE_DIR, "settings.json")
BACKUP_FILE = SETTINGS_FILE + ".bak"

VOICES = {
    "female": os.path.join(VOICES_DIR, "en_US-amy-medium.onnx"),
    "male":   os.path.join(VOICES_DIR, "en_US-ryan-medium.onnx"),
    "child":  os.path.join(VOICES_DIR, "en_US-lessac-low.onnx"),
}

DEFAULT = {
    "language":      "en",
    "voice":         "female",
    "speed":         "150",
    "ai_model":      "gpt-5.4-nano",
    "robot_name":    "Pi Assistant",
    "welcome_speech": "System ready",
    # Extra spellings that should wake Giya, e.g. ["giyya", "geeya"].
    # Add what the [WAKE] near-miss log lines show - no code change needed.
    "wake_words_extra": [],
}

_lock = threading.RLock()


def _read(path):
    try:
        with open(path, "r") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _write_atomic(path, data):
    # Temp file, flushed to the card, then renamed: a power cut leaves the old
    # settings or the new ones - never an empty file (what happened on Ben).
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def load_settings():
    with _lock:
        data = _read(SETTINGS_FILE)
        if data is None and os.path.exists(SETTINGS_FILE):
            print("[SETTINGS] settings.json is empty or damaged - using backup")
            data = _read(BACKUP_FILE)
            if data is not None:
                try:
                    _write_atomic(SETTINGS_FILE, data)
                except OSError:
                    pass
        merged = DEFAULT.copy()
        if data:
            merged.update(data)
        return merged


def save_settings(data):
    with _lock:
        s = load_settings()
        s.update(data)
        _write_atomic(SETTINGS_FILE, s)
        try:
            _write_atomic(BACKUP_FILE, s)
        except OSError:
            pass
        return s


def get_voice_model():
    s = load_settings()
    voice = s.get("voice", "female")
    return VOICES.get(voice, VOICES["female"])
