"""
identity.py — HUTARK factory/QC robot identity (read-only at runtime).

robot_identity.json is written ONCE, at manufacturing/QC time, by whatever
tooling performs that step (not this file). This module only ever READS it.
No route in server.py writes to this file — see robot_identity.json and
README.md for how it's populated.

Deliberately a separate file/module from settings.py: settings.json holds
customer-editable data (robot_name, welcome_speech, voice, chatgpt_enabled);
robot_identity.json holds the permanent factory identity. They must never
share a source, so a customer's /save-audio or /settings request can never
change what /status reports as robot_id/robot_type/tier.

If robot_identity.json is missing or invalid, load_identity() returns None
and /status omits the identity fields entirely — never a placeholder value,
which could otherwise be mistaken by the app for a genuine identity.

T-24 addition: robot_model and serial_number are optional manufacturing
fields, included only when present in robot_identity.json. Existing readers
of robot_id/robot_type/tier/protocol_version are unaffected — this is a
purely additive change to the dict this function returns.
"""
import json, os

# Self-locating, NOT hardcoded to /home/groot/pi_assistant.
#
# That path is Luna's. On Giya the code lives under /home/giya, so a hardcoded
# path means the file is simply never found — and the failure is silent:
# load_identity() returns None and /status omits the identity fields, which
# looks exactly like a missing identity file rather than a wrong path.
#
# It also breaks the app-to-robot check, which is the whole point of the
# protocol: with robot_type unknown, /control/acquire cannot tell a Luna app
# from a Giya one and lets either in.
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
IDENTITY_FILE = f"{BASE_DIR}/robot_identity.json"


def load_identity():
    if not os.path.exists(IDENTITY_FILE):
        return None
    try:
        with open(IDENTITY_FILE, "r") as f:
            data = json.load(f)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    robot_id = data.get("robot_id")
    if not isinstance(robot_id, str) or not robot_id.strip():
        return None
    result = {
        "robot_id": robot_id.strip(),
        "robot_type": str(data.get("robot_type", "")).strip() or "unknown",
        "tier": str(data.get("tier", "")).strip() or "unknown",
        "protocol_version": str(data.get("protocol_version", "1")).strip() or "1",
    }
    robot_model = data.get("robot_model")
    if isinstance(robot_model, str) and robot_model.strip():
        result["robot_model"] = robot_model.strip()
    serial_number = data.get("serial_number")
    if isinstance(serial_number, str) and serial_number.strip():
        result["serial_number"] = serial_number.strip()
    return result
