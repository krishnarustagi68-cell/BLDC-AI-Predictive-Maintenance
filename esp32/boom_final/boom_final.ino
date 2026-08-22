#include <Wire.h>
#include <SparkFun_ISM330DHCX.h>
#include <SparkFun_MMC5983MA_Arduino_Library.h>
#include <Adafruit_INA219.h>
#include <Adafruit_SSD1306.h>
#include <Adafruit_GFX.h>

// ── Pins ─────────────────────────────────────────────────────
#define SDA0 21
#define SCL0 22
#define SDA1 33
#define SCL1 32
#define LM35_PIN 34
#define IR_PIN 27
#define ESC_PIN 18

// ── ESC (matching WORKING code exactly) ──────────────────────
#define ESC_FREQ 50
#define ESC_RES 16
#define ESC_MIN 3277  // 1.0ms arming
#define ESC_SPD 6000  // Running speed

// ── Detection Thresholds (Matched to your real dataset) ──────
#define THRESH_VIBZ 0.0015f  // > 0.0015 = Misalignment
#define THRESH_VIBX 0.0085f  // > 0.0085 = Misalignment
#define THRESH_OVL_RPM 1290.0f  // < 1290 = Overload
#define THRESH_TEMP 45.0f       // > 45 = Overheating

// ── Objects ──────────────────────────────────────────────────
SparkFun_ISM330DHCX imu;
SFE_MMC5983MA mag;
Adafruit_INA219 ina219;
Adafruit_SSD1306 oled1(128, 64, &Wire, -1);
Adafruit_SSD1306 oled2(128, 64, &Wire1, -1);
sfe_ism_data_t accelData;

volatile unsigned long pulseCount = 0;
unsigned long lastRPMTime = 0;
float rpm = 0.0;
int rowCount = 0;

float g_temp = 0, g_ax = 0, g_ay = 0, g_az = 0;
float g_cur = 0, g_volt = 0;
float g_mx = 0, g_my = 0, g_mz = 0;

bool imuOK = false, magOK = false, inaOK = false;

// ── RPM ISR ──────────────────────────────────────────────────
void IRAM_ATTR onIRPulse() {
  static unsigned long last = 0;
  unsigned long now = millis();
  if (now - last > 5) {
    pulseCount++;
    last = now;
  }
}

// ── Sensor Init ──────────────────────────────────────────────
bool initISM() {
  if (!imu.begin()) return false;
  imu.deviceReset(); delay(25);
  while (!imu.getDeviceReset()) {}
  imu.setDeviceConfig();
  imu.setBlockDataUpdate();
  imu.setAccelDataRate(ISM_XL_ODR_104Hz);
  imu.setAccelFullScale(ISM_4g);
  imu.setGyroDataRate(ISM_GY_ODR_104Hz);
  imu.setGyroFullScale(ISM_250dps);
  imu.setAccelFilterLP2();
  imu.setAccelSlopeFilter(ISM_LP_ODR_DIV_100);
  return true;
}

bool initMAG() {
  if (!mag.begin()) return false;
  mag.softReset(); delay(10);
  mag.setFilterBandwidth(100);
  mag.enableAutomaticSetReset();
  return true;
}

float readTemp() {
  float mV = (analogRead(LM35_PIN) / 4095.0) * 3300.0;
  float t = mV / 10.0;
  return (t < -10 || t > 150) ? 0.0 : t;
}

void readVib(float &ax, float &ay, float &az) {
  if (imuOK && imu.checkStatus()) {
    imu.getAccel(&accelData);
    ax = accelData.xData / 1000.0;
    ay = accelData.yData / 1000.0;
    az = accelData.zData / 1000.0;
  } else {
    ax = ay = az = 0.0;
  }
}

void readMag(float &mx, float &my, float &mz) {
  if (magOK) {
    uint32_t rx, ry, rz;
    mag.getMeasurementXYZ(&rx, &ry, &rz);
    mx = ((float)rx - 131072.0) / 16384.0;
    my = ((float)ry - 131072.0) / 16384.0;
    mz = ((float)rz - 131072.0) / 16384.0;
  } else {
    mx = my = mz = 0.0;
  }
}

// ── Fault Detection (Matches Python GUI Logic) ───────────────
String detectFault() {
  float vz = abs(g_az);
  float vx = abs(g_ax);
  
  int misalign_score = 0;
  if (vz > THRESH_VIBZ) misalign_score += 2;
  if (vx > THRESH_VIBX) misalign_score += 1;
  if (g_mz > -0.125) misalign_score += 1; // MagZ becomes less negative
  
  if (misalign_score >= 2) return "Misalignment";
  if (rpm > 0 && rpm < THRESH_OVL_RPM) return "Overload";
  if (g_temp > THRESH_TEMP) return "Overheating";
  return "Normal";
}

// ═════════════════════════════════════════════════════════════
// SETUP
// ═════════════════════════════════════════════════════════════
void setup() {
  Serial.begin(115200);
  delay(500);
  Serial.println("=== DATA COLLECTION MODE ===");

  // ESC FIRST
  ledcAttach(ESC_PIN, ESC_FREQ, ESC_RES);
  ledcWrite(ESC_PIN, ESC_MIN);
  delay(5000); // Arm
  
  for (int s = ESC_MIN; s <= ESC_SPD; s += 25) {
    ledcWrite(ESC_PIN, s);
    delay(100);
  }
  Serial.println("[ESC] Motor running!");

  // I2C
  Wire.begin(SDA0, SCL0); Wire.setClock(400000);
  Wire1.begin(SDA1, SCL1); Wire1.setClock(400000);
  delay(100);

  // Sensors (Non-blocking)
  imuOK = initISM();
  magOK = initMAG();
  inaOK = ina219.begin(&Wire1); // FIX: Wire1!
  if (inaOK) ina219.setCalibration_32V_2A();

  // OLEDs
  oled1.begin(SSD1306_SWITCHCAPVCC, 0x3C);
  oled2.begin(SSD1306_SWITCHCAPVCC, 0x3C);

  // IR
  pinMode(IR_PIN, INPUT_PULLUP);
  attachInterrupt(digitalPinToInterrupt(IR_PIN), onIRPulse, RISING);
  lastRPMTime = millis();

  Serial.println("READY");
  Serial.println("Temperature,VibrationX,VibrationY,VibrationZ,Current,Voltage,RPM,MagX,MagY,MagZ");
}

// ═════════════════════════════════════════════════════════════
// LOOP
// ═════════════════════════════════════════════════════════════
void loop() {
  unsigned long now = millis();

  // RPM
  if (now - lastRPMTime >= 1000) {
    noInterrupts();
    unsigned long c = pulseCount;
    pulseCount = 0;
    interrupts();
    rpm = (c / ((now - lastRPMTime) / 1000.0)) * 60.0 / 6.0;
    lastRPMTime = now;
  }

  // Read Sensors
  g_temp = readTemp();
  readVib(g_ax, g_ay, g_az);
  if (inaOK) {
    g_cur = max(0.0f, ina219.getCurrent_mA() / 1000.0f);
    g_volt = ina219.getBusVoltage_V();
  } else {
    g_cur = 0.0f; g_volt = 0.0f;
  }
  readMag(g_mx, g_my, g_mz);

  // Output CSV to Python Script
  Serial.printf("%.2f,%.4f,%.4f,%.4f,%.4f,%.2f,%.1f,%.3f,%.3f,%.3f\n",
                g_temp, g_ax, g_ay, g_az, g_cur, g_volt, rpm, g_mx, g_my, g_mz);
  rowCount++;

  // Detect Fault for OLED
  String fault = detectFault();

  // ── OLED 1: Sensor Data ────────────────────────
  oled1.clearDisplay();
  oled1.setTextSize(1);
  oled1.setTextColor(WHITE);
  
  oled1.setCursor(0, 0);
  oled1.printf("T:%.1fC  V:%.2fV", g_temp, g_volt);
  
  oled1.setCursor(0, 10);
  oled1.printf("I:%.3fA RPM:%.0f", g_cur, rpm);
  
  oled1.setCursor(0, 20);
  oled1.printf("Vx:%.4f Vy:%.4f", g_ax, g_ay);
  
  oled1.setCursor(0, 30);
  oled1.printf("Vz:%.4f", g_az);
  
  oled1.setCursor(0, 54);
  oled1.printf("Row #%d", rowCount);
  oled1.display();

  // ── OLED 2: Fault Condition + Magnetometer ─────
  oled2.clearDisplay();
  oled2.setTextColor(WHITE);
  
  // Big fault status text
  oled2.setTextSize(2);
  oled2.setCursor(0, 0);
  if (fault == "Normal") oled2.println("NORMAL");
  else if (fault == "Misalignment") oled2.println("MISALIGN");
  else if (fault == "Overload") oled2.println("OVERLOAD");
  else if (fault == "Overheating") oled2.println("HOT!");
  
  oled2.drawLine(0, 18, 127, 18, WHITE);
  
  // Magnetometer data
  oled2.setTextSize(1);
  oled2.setCursor(0, 24);
  oled2.printf("Mx:%.2f My:%.2f", g_mx, g_my);
  
  oled2.setCursor(0, 36);
  oled2.printf("Mz:%.2f", g_mz);
  
  oled2.setCursor(0, 54);
  oled2.printf("Collecting...");
  oled2.display();

  delay(500);
}
