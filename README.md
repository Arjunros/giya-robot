# Giya Robot — Complete Setup Guide

## Overview
Giya is an AI-powered humanoid robot built on Raspberry Pi 5 with ESP32 WROOM for motor and servo control, TF-Luna LiDAR for obstacle and person detection, INMP441 microphone for voice input, and WS2812 LED eyes.

---

## Hardware Requirements

| Component | Specification |
|-----------|--------------|
| Raspberry Pi 5 | 8GB RAM |
| MicroSD Card | 64GB Class 10 |
| ESP32 WROOM | CP2102 USB chip |
| Servo Motor | MG996R x2 (Forearm L/R) |
| Motor Driver | BTN7960 / IBT-2 |
| Distance Sensor | TF-Luna LiDAR (I2C mode) |
| Microphone | INMP441 (I2S) |
| Speaker | USB Soundcard |
| LED Eyes | WS2812 NeoPixel Ring x2 (9 LEDs each) |
| Power Supply | 5V 10A external (motors and servos) |

---

## ESP32 WROOM Pin Map

| GPIO | Function |
|------|----------|
| 27 | RPWM_L Left Motor Forward |
| 25 | LPWM_L Left Motor Backward |
| 13 | RPWM_R Right Motor Forward |
| 15 | LPWM_R Right Motor Backward |
| 26 | EN_L Left Motor Enable |
| 14 | EN_R Right Motor Enable |
| 32 | Servo Left Forearm |
| 33 | Servo Right Forearm |
| 21 | SDA TF-Luna I2C |
| 22 | SCL TF-Luna I2C |
| 16 | RX2 Pi Serial TX |
| 17 | TX2 Pi Serial RX |

---

## Raspberry Pi GPIO Map

| Component | Pi GPIO |
|-----------|---------|
| WS2812 Left Eye | GPIO 24 |
| WS2812 Right Eye | GPIO 10 |
| INMP441 SD | GPIO 20 |
| INMP441 WS | GPIO 19 |
| INMP441 SCK | GPIO 18 |
| INMP441 L/R | GND |
| ESP32 TX (pin 17) | Pi RX ttyAMA0 |
| ESP32 RX (pin 16) | Pi TX ttyAMA0 |

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
5. Open esp32/giya_esp32.ino
6. Board: ESP32 Dev Module
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
| /speed | GET | Motor speed (value=0-100) |
| /position | GET | Servo position (part=forearm, value=0-2000, hand=left/right/both) |
| /home | GET | Home servos |
| /eyes1 | GET | Eye mode 1 calm blue |
| /eyes2 | GET | Eye mode 2 heartbeat |
| /eyes3 | GET | Eye mode 3 rainbow |
| /shutdown | GET | Safe shutdown |
| /restart | GET | Reboot Pi |

---

## Wake Word

- Say robot name (default: **Giya**) to activate Q&A mode
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
| Motors not moving | Check EN pins HIGH. Check power supply |
| Service not starting | Run sudo journalctl -u piassistant -n 50 |
| TF-Luna not detecting | Check I2C wiring SDA=21 SCL=22 on ESP32 |
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
