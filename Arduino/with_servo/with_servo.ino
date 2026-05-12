/*
  GIYA ROBOT - FULL CODE (WORKING VERSION)

  This version keeps the proven servo control intact and includes:
  - UART communication with Raspberry Pi (RX=16, TX=17)
  - Forearm servo control
      Left  servo -> GPIO 32
      Right servo -> GPIO 27
  - TF-Luna I2C distance sensor
  - DC motor control using plain digital HIGH/LOW (no PWM)
    to avoid LEDC conflicts with ESP32Servo

  IMPORTANT:
  - Servos are fully working.
  - Motors run at full speed only.
  - If you need variable motor speed later, use a second ESP32
    or a PCA9685 PWM driver.
*/

#include <HardwareSerial.h>
#include <Wire.h>
#include <ESP32Servo.h>

// ──────────────────────────────────────────────────────────
// MOTOR PINS
// ──────────────────────────────────────────────────────────
#define RPWM_L  33
#define LPWM_L  25
#define RPWM_R  13
#define LPWM_R  15
#define EN_L    26
#define EN_R    2

// ──────────────────────────────────────────────────────────
// SERVO PINS
// ──────────────────────────────────────────────────────────
#define SERVO_L_PIN  32   // Left forearm
#define SERVO_R_PIN  27   // Right forearm

// ──────────────────────────────────────────────────────────
// TF-LUNA I2C
// ──────────────────────────────────────────────────────────
#define SDA_PIN        21
#define SCL_PIN        22
#define LUNA_ADDR      0x10
#define OBSTACLE_DIST  50
#define DETECT_DIST    150
#define COOLDOWN_MS    10000

// ──────────────────────────────────────────────────────────
// SERVO SETTINGS
// ──────────────────────────────────────────────────────────
#define SERVO_MIN    0
#define SERVO_MAX    180
#define SERVO_CENTER 90

// ──────────────────────────────────────────────────────────
// UART TO RASPBERRY PI
// ──────────────────────────────────────────────────────────
HardwareSerial PiSerial(2);   // RX=16, TX=17

// ──────────────────────────────────────────────────────────
// SERVO OBJECTS
// ──────────────────────────────────────────────────────────
Servo servoL;
Servo servoR;

// ──────────────────────────────────────────────────────────
// GLOBAL VARIABLES
// ──────────────────────────────────────────────────────────
int           currentSpeed   = 100;   // kept for compatibility
int           lunaDistance   = 999;
bool          obstacleAhead  = false;
char          currentDir[12] = "stop";
char          savedDir[12]   = "stop";
unsigned long lastGreeted    = 0;
unsigned long lastLuna       = 0;

// ──────────────────────────────────────────────────────────
// HELPER FUNCTIONS
// ──────────────────────────────────────────────────────────

// Convert slider value (0–2000) to servo angle (0–180)
int sliderToAngle(int value) {
  value = constrain(value, 0, 2000);
  return map(value, 0, 2000, SERVO_MIN, SERVO_MAX);
}

// ──────────────────────────────────────────────────────────
// SERVO FUNCTIONS
// ──────────────────────────────────────────────────────────
void homeServos() {
  servoL.write(SERVO_CENTER);
  servoR.write(SERVO_CENTER);
  Serial.println("[SERVO] HOMED TO 90");
}

void moveServos(int value, const char* hand) {
  int angle = sliderToAngle(value);

  Serial.print("[SERVO] hand=");
  Serial.print(hand);
  Serial.print(" value=");
  Serial.print(value);
  Serial.print(" angle=");
  Serial.println(angle);

  if (strcmp(hand, "left") == 0) {
    servoL.write(angle);
  }
  else if (strcmp(hand, "right") == 0) {
    servoR.write(angle);
  }
  else if (strcmp(hand, "both") == 0) {
    servoL.write(angle);
    servoR.write(angle);
  }
}

// ──────────────────────────────────────────────────────────
// MOTOR FUNCTIONS (FULL SPEED ONLY, NO PWM)
// ──────────────────────────────────────────────────────────
void stopMotors() {
  digitalWrite(RPWM_L, LOW);
  digitalWrite(LPWM_L, LOW);
  digitalWrite(RPWM_R, LOW);
  digitalWrite(LPWM_R, LOW);

  strcpy(currentDir, "stop");
  Serial.println("[MOTOR] STOP");
}

void executeMove(const char* dir) {
  strncpy(currentDir, dir, sizeof(currentDir) - 1);
  currentDir[sizeof(currentDir) - 1] = '\0';

  // Prevent forward motion if obstacle detected
  if (strcmp(dir, "forward") == 0 && obstacleAhead) {
    stopMotors();
    PiSerial.print("BLOCKED:");
    PiSerial.println(lunaDistance);
    return;
  }

  if (strcmp(dir, "forward") == 0) {
    digitalWrite(RPWM_L, HIGH);
    digitalWrite(LPWM_L, LOW);
    digitalWrite(RPWM_R, HIGH);
    digitalWrite(LPWM_R, LOW);
    Serial.println("[MOTOR] FORWARD");
  }
  else if (strcmp(dir, "backward") == 0) {
    digitalWrite(RPWM_L, LOW);
    digitalWrite(LPWM_L, HIGH);
    digitalWrite(RPWM_R, LOW);
    digitalWrite(LPWM_R, HIGH);
    Serial.println("[MOTOR] BACKWARD");
  }
  else if (strcmp(dir, "left") == 0) {
    digitalWrite(RPWM_L, LOW);
    digitalWrite(LPWM_L, HIGH);
    digitalWrite(RPWM_R, HIGH);
    digitalWrite(LPWM_R, LOW);
    Serial.println("[MOTOR] LEFT");
  }
  else if (strcmp(dir, "right") == 0) {
    digitalWrite(RPWM_L, HIGH);
    digitalWrite(LPWM_L, LOW);
    digitalWrite(RPWM_R, LOW);
    digitalWrite(LPWM_R, HIGH);
    Serial.println("[MOTOR] RIGHT");
  }
  else {
    stopMotors();
  }
}

// ──────────────────────────────────────────────────────────
// TF-LUNA FUNCTIONS
// ──────────────────────────────────────────────────────────
void parseLuna() {
  if (millis() - lastLuna < 100) return;
  lastLuna = millis();

  Wire.beginTransmission(LUNA_ADDR);
  Wire.write(0x00);
  if (Wire.endTransmission(false) != 0) return;

  Wire.requestFrom(LUNA_ADDR, 9);
  if (Wire.available() < 9) return;

  uint8_t data[9];
  for (int i = 0; i < 9; i++) {
    data[i] = Wire.read();
  }

  int dist = data[0] | (data[1] << 8);
  if (dist <= 0 || dist >= 800) return;

  bool wasBlocked = obstacleAhead;
  lunaDistance = dist;
  obstacleAhead = (dist <= OBSTACLE_DIST);

  // Obstacle detected
  if (obstacleAhead && !wasBlocked) {
    strcpy(savedDir, currentDir);
    stopMotors();
    PiSerial.print("OBSTACLE:");
    PiSerial.println(dist);
    Serial.print("[LUNA] OBSTACLE: ");
    Serial.println(dist);
  }

  // Obstacle cleared
  if (!obstacleAhead && wasBlocked) {
    PiSerial.println("CLEAR:resuming");
    Serial.println("[LUNA] CLEAR");
  }

  // Person detected
  unsigned long now = millis();
  if (dist >= 10 && dist <= DETECT_DIST &&
      (now - lastGreeted > COOLDOWN_MS)) {
    lastGreeted = now;
    strcpy(savedDir, currentDir);
    stopMotors();
    PiSerial.print("PERSON_DETECTED:");
    PiSerial.println(dist);
    Serial.print("[LUNA] PERSON: ");
    Serial.println(dist);
  }
}

// ──────────────────────────────────────────────────────────
// SERIAL COMMAND HANDLER
// ──────────────────────────────────────────────────────────
void handleSerial() {
  if (!PiSerial.available()) return;

  String cmd = PiSerial.readStringUntil('\n');
  cmd.trim();

  if (cmd.length() == 0) return;

  Serial.print("[CMD] ");
  Serial.println(cmd);

  // MOVE:forward
  if (cmd.startsWith("MOVE:")) {
    executeMove(cmd.substring(5).c_str());
    return;
  }

  // SPEED / TOPSPEED (ignored in digital mode, accepted for compatibility)
  if (cmd.startsWith("SPEED:") || cmd.startsWith("TOPSPEED:")) {
    Serial.println("[SPEED] Ignored (motors run full speed)");
    return;
  }

  // HOME
  if (cmd == "HOME" ||
      cmd == "HOME:left" ||
      cmd == "HOME:right") {
    homeServos();
    return;
  }

  // RESUME
  if (cmd == "RESUME") {
    executeMove(savedDir);
    PiSerial.println("Resumed");
    return;
  }

  // POS:forearm:2000:left
  if (!cmd.startsWith("POS:")) return;

  int p1 = cmd.indexOf(':', 4);
  if (p1 < 0) return;

  int p2 = cmd.indexOf(':', p1 + 1);
  if (p2 < 0) return;

  String part = cmd.substring(4, p1);
  int value   = cmd.substring(p1 + 1, p2).toInt();
  String hand = cmd.substring(p2 + 1);

  if (part == "forearm") {
    moveServos(value, hand.c_str());
  } else {
    Serial.print("[IGNORE] Unknown part: ");
    Serial.println(part);
  }
}

// ──────────────────────────────────────────────────────────
// SETUP
// ──────────────────────────────────────────────────────────
void setup() {
  Serial.begin(115200);
  delay(1000);

  // UART to Raspberry Pi
  PiSerial.begin(115200, SERIAL_8N1, 16, 17);

  // I2C for TF-Luna
  Wire.begin(SDA_PIN, SCL_PIN);
  Wire.setClock(100000);
  Serial.println("[LUNA] I2C started");

  // Initialize motors as simple digital outputs
  pinMode(EN_L, OUTPUT);
  pinMode(EN_R, OUTPUT);
  pinMode(RPWM_L, OUTPUT);
  pinMode(LPWM_L, OUTPUT);
  pinMode(RPWM_R, OUTPUT);
  pinMode(LPWM_R, OUTPUT);

  digitalWrite(EN_L, HIGH);
  digitalWrite(EN_R, HIGH);

  stopMotors();

  // Initialize servos
  ESP32PWM::allocateTimer(0);
  ESP32PWM::allocateTimer(1);
  ESP32PWM::allocateTimer(2);
  ESP32PWM::allocateTimer(3);

  servoL.setPeriodHertz(50);
  servoR.setPeriodHertz(50);

  servoL.attach(SERVO_L_PIN, 500, 2400);
  servoR.attach(SERVO_R_PIN, 500, 2400);

  homeServos();

  Serial.println("Giya ready.");
  PiSerial.println("Giya ready.");
}

// ──────────────────────────────────────────────────────────
// LOOP
// ──────────────────────────────────────────────────────────
void loop() {
  parseLuna();
  handleSerial();

  static unsigned long lastPrint = 0;
  if (millis() - lastPrint > 2000) {
    lastPrint = millis();

    Serial.print("Dist:");
    Serial.print(lunaDistance);
    Serial.print(" Obs:");
    Serial.print(obstacleAhead);
    Serial.print(" Dir:");
    Serial.println(currentDir);
  }
}
