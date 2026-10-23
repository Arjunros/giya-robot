# Giya Robot — Complete Setup Guide

## Overview
Giya is an AI-powered humanoid robot built on Raspberry Pi 5 with an ESP32-S3 for motor, servo and power control, TF-Luna LiDAR for obstacle and person detection, a wireless lavalier mic (INMP441 as fallback) for voice input, and WS2812 LED eyes.

---

## Hardware Requirements

| Component | Specification |
|-----------|--------------|
| Raspberry Pi 5 | 8GB RAM |
| MicroSD Card | 64GB Class 10 |
| ESP32-S3 | DevKit (motors, servos, TF-Luna, power latch) |
| Servo Motor | MG996R x2 (Forearm L/R) |
| Motor Driver | BTS7960 / IBT-2 x2 |
| Distance Sensor | TF-Luna LiDAR (I2C mode) |
| Microphone | Wireless lav USB receiver (4c4a:4155), INMP441 (I2S) fallback |
| Speaker | USB soundcard (PCM2902, 08bb:2902) |
| WiFi dongle | USB WiFi adapter on wlan1 for the GiyaRobot hotspot |
| Power switch | Latching power circuit held by the ESP32 + momentary push button |
| LED Eyes | WS2812 NeoPixel Ring x2 (9 LEDs each) |
| Power Supply | 5V 10A external (motors and servos) |

---

## ESP32-S3 Pin Map

Matches `Arduino/final_giya_firmware.ino`.

| GPIO | Function | Connects to |
|------|----------|-------------|
| 10 | RPWM_L Left motor forward | Left BTS7960 RPWM |
| 9  | LPWM_L Left motor backward | Left BTS7960 LPWM |
| 3  | EN_L Left motor enable | Left BTS7960 R_EN + L_EN |
| 13 | RPWM_R Right motor forward | Right BTS7960 RPWM |
| 12 | LPWM_R Right motor backward | Right BTS7960 LPWM |
| 11 | EN_R Right motor enable | Right BTS7960 R_EN + L_EN |
| 6  | Servo left forearm | MG996R signal |
| 7  | Servo right forearm | MG996R signal |
| 14 | SDA, I2C | TF-Luna SDA |
| 21 | SCL, I2C | TF-Luna SCL |
| 18 | UART2 RX | Pi TX, GPIO 14 (physical pin 8) |
| 17 | UART2 TX | Pi RX, GPIO 15 (physical pin 10) |
| 35 | LATCH, HIGH holds the power on | Power latch circuit |
| 15 | Shutdown button (INPUT_PULLDOWN, pressed = HIGH) | Push button to 3.3V |

Notes:
- **GND** of the ESP32, Pi, motor drivers and TF-Luna must all be connected.
- **TF-Luna** must be in I2C mode (address `0x10`).
- **Serial** runs at 115200 baud. ESP32 TX goes to Pi RX and ESP32 RX to Pi TX; they cross over.
- **GPIO 35:** on ESP32-S3 modules with octal PSRAM (N8R8 / N16R8), GPIO 35–37 are used by the PSRAM. If the latch misbehaves on such a module, move it to a free pin and change `LATCH_PIN`.

### Power button and latch

| Action | What happens |
|--------|--------------|
| Short press (robot off) | Board powers up, ESP32 sets GPIO 35 HIGH at once and holds the power |
| Hold 3 s (robot on) | ESP32 sends `SHUTDOWN` to the Pi, Pi says goodbye and shuts down, ESP32 cuts power 15 s later |
| Shutdown from the app | Pi sends `LATCH:OFF`, ESP32 replies `LATCH:COUNTDOWN` and cuts power 15 s later |

The press that switches the robot on is ignored as a shutdown press, so holding the button a little long at power-up does not turn it straight off again.

---

## Raspberry Pi GPIO Map

| Component | Pi GPIO |
|-----------|---------|
| WS2812 Left Eye (data) | GPIO 12 |
| WS2812 Right Eye (data) | GPIO 10 |
| INMP441 SD | GPIO 20 |
| INMP441 WS | GPIO 19 |
| INMP441 SCK | GPIO 18 |
| INMP441 L/R | GND |
| ESP32 TX (GPIO 17) | GPIO 15 RX, ttyAMA0 (pin 10) |
| ESP32 RX (GPIO 18) | GPIO 14 TX, ttyAMA0 (pin 8) |

Eye pins are set in `eyes.py` (`board.D12`, `board.D10`).

### Network

| Interface | Role |
|-----------|------|
| wlan1 (USB dongle) | GiyaRobot hotspot, 192.168.4.1. The phone app connects here |
| wlan0 (built-in) | Internet WiFi. Changed from the app (Settings → WiFi) |

### Eyes

| Colour | Meaning |
|--------|---------|
| Breathing orange | App not connected |
| Breathing blue (or the colour picked with the app's slider) | App connected |
| Green | Listening |
| Cyan | Speaking |
| Orange flashing | Thinking |
| Red flashing | Obstacle / shutting down |

---

## Step 1 — Flash Raspberry Pi OS

1. Download Raspberry Pi Imager from https://www.raspberrypi.com/software
2. Select Raspberry Pi OS Bookworm 64-bit
3. Advanced settings:
   - Enable SSH
   - Username: ben
   - Set password
4. Flash to MicroSD and boot

---

## Step 2 — System Packages

```bash
sudo apt update && sudo apt upgrade -y

sudo apt install -y python3-pip python3-venv git ffmpeg sox \
    libportaudio2 portaudio19-dev libsndfile1 \
    python3-serial alsa-utils udev \
    python3-lgpio python3-rpi.gpio \
    swig python3-dev libgpiod-dev
```

---

## Step 3 — Enable I2S for INMP441

```bash
sudo nano /boot/firmware/config.txt
```

Add at bottom:
dtparam=i2s=on
dtoverlay=googlevoicehat-soundcard

Reboot:
```bash
sudo reboot
```

Verify:
```bash
arecord -l
# Should show: sndrpigooglevoi
```

---

## Step 4 — Enable Serial Port for ESP32

```bash
sudo raspi-config
```

Interface Options → Serial Port:
- Login shell over serial: NO
- Serial hardware enabled: YES

Reboot and verify:
```bash
ls -la /dev/ttyAMA0
```

---

## Step 5 — Install Piper TTS

```bash
cd /tmp
wget https://github.com/rhasspy/piper/releases/download/v1.2.0/piper_arm64.tar.gz
tar -xzf piper_arm64.tar.gz
sudo cp piper/piper /usr/local/bin/
```

Download voice model:
```bash
mkdir -p ~/pi_assistant/voices
cd ~/pi_assistant/voices

wget https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/amy/medium/en_US-amy-medium.onnx
wget https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/amy/medium/en_US-amy-medium.onnx.json
```

Test:
```bash
echo "Hello I am Giya" | piper \
  --model ~/pi_assistant/voices/en_US-amy-medium.onnx \
  --output_raw | aplay -r 22050 -f S16_LE -c 1 -
```

---

## Step 6 — Clone Repository

```bash
git clone https://github.com/Arjunros/giya-robot.git ~/pi_assistant
cd ~/pi_assistant
```

---

## Step 7 — Python Virtual Environment

```bash
cd ~/pi_assistant
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

Fix lgpio if needed:
```bash
cp /usr/lib/python3/dist-packages/lgpio.py \
   ~/pi_assistant/venv/lib/python3.*/site-packages/
```

---

## Step 8 — Create Config Files

```bash
cd ~/pi_assistant

cp settings.default.json settings.json
cp qa_store.default.json qa_store.json
cp poses.default.json poses.json
```

---

## Step 9 — WiFi Hotspot

```bash
sudo nmcli device wifi hotspot \
  ssid GiyaRobot \
  password giya1234 \
  ifname wlan0

sudo nmcli connection modify GiyaRobot connection.autoconnect yes
```

Pi IP: **192.168.4.1**
App URL: **http://192.168.4.1:5000**

---

## Step 10 — Install Systemd Service

```bash
sudo cp setup/piassistant.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable piassistant
sudo systemctl start piassistant
```

Check logs:
```bash
sudo journalctl -u piassistant -f
```

---

## Step 11 — Upload ESP32 Code

1. Install Arduino IDE from https://www.arduino.cc/en/software
2. Add ESP32 board URL in Preferences:https://raw.githubusercontent.com/espressif/arduino-esp32/gh-pages/package_esp32_index.json
3. Board Manager → install ESP32 by Espressif
4. Library Manager → install ESP32Servo
5. Open Arduino/final_giya_firmware.ino
6. Board: ESP32S3 Dev Module
7. Upload

---

## Step 12 — Verify Everything

```bash
# Service status
sudo systemctl status piassistant

# ESP32 connected
ls -la /dev/ttyAMA0

# Audio devices
arecord -l && aplay -l

# API test
curl http://localhost:5000/ping
```

Expected:
```json
{"status": "ok", "message": "Pi is alive"}
```

---

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| /ping | GET | Health check |
| /status | GET | Robot status |
| /save-audio | POST | Save robot name, welcome speech, Q&As |
| /qa/list | GET | List all Q&As |
| /qa/add | POST | Add Q&A pair |
| /qa/delete | POST | Delete Q&A |
| /move | GET | Motor control (dir=forward/backward/left/right/stop) |
| /speed | GET | Motor speed (value=0-200, 0 = stop) |
| /position | GET | Servo position (part=forearm, value=0-2000, hand=left/right/both) |
| /home | GET | Home servos |
| /eyes1 | GET | Eye mode 1 calm blue |
| /eyes2 | GET | Eye mode 2 heartbeat |
| /eyes3 | GET | Eye mode 3 rainbow |
| /eyes/color | GET | Breathing eye colour (r, g, b = 0-255), saved across reboots |
| /settings | GET | Settings; also volume=, and wifi_ssid= + wifi_password= to change the internet WiFi |
| /control/acquire | POST | App takes control (one app at a time); returns session_id |
| /control/heartbeat | POST | Keeps the app's session alive |
| /control/release | POST | App gives up control |
| /shutdown | GET | Safe shutdown |
| /restart | GET | Reboot Pi |

---

## Wake Word

- Say robot name (default: **Giya**) to activate Q&A mode
- Common mishearings (geeya, jiya, gia, Gaia...) also wake her
- To add another spelling, put it in `settings.json` under `"wake_words_extra": ["word"]`. No restart needed. Words that nearly matched are logged: `journalctl -u piassistant | grep "near miss"`
- Say **hi** to activate face recognition mode
- Robot name can be changed via app

---

## Troubleshooting

| Problem | Solution |
|---------|----------|
| No audio output | Check USB soundcard. Run aplay -l |
| Mic not recording | Check INMP441 wiring. Run arecord -l |
| ESP32 not connecting | Check /dev/ttyAMA0. Enable serial in raspi-config |
| Wake word not working | Check settings.json robot_name |
| Motors not moving | Check EN pins (GPIO 3, 11) go HIGH. Check power supply |
| ESP32 not answering | TX/RX crossed? ESP32 GPIO 17 to Pi GPIO 15, ESP32 GPIO 18 to Pi GPIO 14 |
| Robot switches off when the button is released | Latch not holding: check the GPIO 35 to latch circuit |
| Service not starting | Run sudo journalctl -u piassistant -n 50 |
| TF-Luna not detecting | Check I2C wiring SDA=14 SCL=21 on ESP32-S3 |
| Eyes not working | Run pip install rpi-lgpio in venv |

---

## Manufacturing Checklist

- [ ] Flash OS and configure username ben
- [ ] Install all system packages
- [ ] Enable I2S overlay for INMP441
- [ ] Enable serial port for ESP32
- [ ] Install Piper TTS and voice model
- [ ] Clone repository
- [ ] Setup Python venv and install requirements
- [ ] Create config files from defaults
- [ ] Setup WiFi hotspot
- [ ] Install systemd service
- [ ] Upload ESP32 code
- [ ] Test all endpoints
- [ ] Test wake word
- [ ] Test motors and servos
- [ ] Test TF-Luna detection
- [ ] Test LED eyes
