#include <HardwareSerial.h>

// BTN7960B pins
#define RPWM_L  26
#define LPWM_L  25
#define RPWM_R  13
#define LPWM_R  15
#define EN_L    33
#define EN_R    2

// TF-Luna
#define LUNA_RX 6
#define LUNA_TX 5

#define OBSTACLE_DISTANCE 50
#define DETECT_DISTANCE   150
#define COOLDOWN_MS       10000
#define PWM_FREQ          5000
#define PWM_RES           8

// Serial1 = Pi communication (TX=17, RX=16)
// Serial  = USB debug monitor
HardwareSerial PiSerial(1);
HardwareSerial LunaSerial(2);

int           currentSpeed  = 150;
int           lunaDistance  = 999;
bool          obstacleAhead = false;
char          currentDir[12] = "stop";
unsigned long lastGreeted   = 0;
unsigned long lastPrint     = 0;

byte lunaBuf[9];
int  lunaIdx  = 0;
bool lunaSync = false;

void stopMotors();
void moveForward();

void parseLuna() {
  while (LunaSerial.available()) {
    byte b = LunaSerial.read();
    if (!lunaSync) {
      if (b == 0x59) { lunaBuf[0]=b; lunaIdx=1; lunaSync=true; }
      continue;
    }
    lunaBuf[lunaIdx++] = b;
    if (lunaIdx==2 && lunaBuf[1]!=0x59) { lunaSync=false; lunaIdx=0; continue; }
    if (lunaIdx==9) {
      lunaSync=false; lunaIdx=0;
      int dist = lunaBuf[3]*256 + lunaBuf[2];
      if (dist>0 && dist<800) {
        bool wasBlocked = obstacleAhead;
        lunaDistance    = dist;
        obstacleAhead   = (dist <= OBSTACLE_DISTANCE);

        if (obstacleAhead && !wasBlocked && strcmp(currentDir,"forward")==0) {
          stopMotors();
          PiSerial.print("OBSTACLE:"); PiSerial.print(dist); PiSerial.println("cm");
          Serial.print("OBSTACLE:"); Serial.println(dist);
        }
        if (!obstacleAhead && wasBlocked && strcmp(currentDir,"forward")==0) {
          moveForward();
          PiSerial.println("CLEAR:resuming");
          Serial.println("CLEAR:resuming");
        }

        unsigned long now = millis();
        if (dist>=10 && dist<=DETECT_DISTANCE && now-lastGreeted>COOLDOWN_MS) {
          lastGreeted = now;
          PiSerial.print("PERSON_DETECTED:"); PiSerial.println(dist);
          Serial.print("PERSON_DETECTED:"); Serial.println(dist);
        }
      }
    }
  }
}

// ── BTN7960B Motor Control ──────────────────────────────────

void stopMotors() {
  ledcWrite(RPWM_L, 0); ledcWrite(LPWM_L, 0);
  ledcWrite(RPWM_R, 0); ledcWrite(LPWM_R, 0);
  Serial.println("STOP");
}

void moveForward() {
  if (obstacleAhead) {
    stopMotors();
    Serial.print("BLOCKED:"); Serial.println(lunaDistance);
    return;
  }
  ledcWrite(RPWM_L, currentSpeed); ledcWrite(LPWM_L, 0);
  ledcWrite(RPWM_R, currentSpeed); ledcWrite(LPWM_R, 0);
  Serial.println("FORWARD");
}

void moveBackward() {
  ledcWrite(RPWM_L, 0); ledcWrite(LPWM_L, currentSpeed);
  ledcWrite(RPWM_R, 0); ledcWrite(LPWM_R, currentSpeed);
  Serial.println("BACKWARD");
}

void turnLeft() {
  ledcWrite(RPWM_L, 0); ledcWrite(LPWM_L, currentSpeed);
  ledcWrite(RPWM_R, currentSpeed); ledcWrite(LPWM_R, 0);
  Serial.println("LEFT");
}

void turnRight() {
  ledcWrite(RPWM_L, currentSpeed); ledcWrite(LPWM_L, 0);
  ledcWrite(RPWM_R, 0); ledcWrite(LPWM_R, currentSpeed);
  Serial.println("RIGHT");
}

void executeMove(const char* dir) {
  if      (strcmp(dir,"forward")==0)  moveForward();
  else if (strcmp(dir,"backward")==0) moveBackward();
  else if (strcmp(dir,"left")==0)     turnLeft();
  else if (strcmp(dir,"right")==0)    turnRight();
  else                                stopMotors();
}

// ── Setup ───────────────────────────────────────────────────

void setup() {
  Serial.begin(115200);                              // USB debug
  PiSerial.begin(115200, SERIAL_8N1, 16, 17);       // Pi UART RX=16, TX=17
  LunaSerial.begin(115200, SERIAL_8N1, LUNA_RX, LUNA_TX); // TF-Luna

  pinMode(EN_L, OUTPUT); digitalWrite(EN_L, HIGH);
  pinMode(EN_R, OUTPUT); digitalWrite(EN_R, HIGH);

  ledcAttach(RPWM_L, PWM_FREQ, PWM_RES);
  ledcAttach(LPWM_L, PWM_FREQ, PWM_RES);
  ledcAttach(RPWM_R, PWM_FREQ, PWM_RES);
  ledcAttach(LPWM_R, PWM_FREQ, PWM_RES);

  stopMotors();
  Serial.println("Giya ready.");
  PiSerial.println("Giya ready.");
}

// ── Loop ────────────────────────────────────────────────────

void loop() {
  parseLuna();

  unsigned long now = millis();
  if (now - lastPrint > 500) {
    lastPrint = now;
    Serial.print("Dist:"); Serial.print(lunaDistance);
    Serial.print(" Obs:"); Serial.print(obstacleAhead);
    Serial.print(" Dir:"); Serial.println(currentDir);
  }

  if (PiSerial.available()) {
    char buf[32];
    int len = 0;
    unsigned long t = millis();
    while (millis()-t < 50 && len < 31) {
      if (PiSerial.available()) {
        char c = PiSerial.read();
        if (c=='\n') break;
        buf[len++] = c;
      }
    }
    buf[len] = '\0';
    if (len>0 && buf[len-1]=='\r') buf[--len]='\0';
    if (len==0) return;

    Serial.print("CMD:"); Serial.println(buf);

    if (strncmp(buf,"MOVE:",5)==0) {
      strncpy(currentDir, buf+5, 11);
      currentDir[11] = '\0';
      executeMove(currentDir);
    }
    else if (strncmp(buf,"SPEED:",6)==0) {
      currentSpeed = map(atoi(buf+6), 0, 100, 0, 255);
    }
    else if (strncmp(buf,"TOPSPEED:",9)==0) {
      currentSpeed = map(atoi(buf+9), 0, 100, 0, 255);
    }
  }
}
