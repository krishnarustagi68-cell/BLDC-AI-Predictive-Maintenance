/*
 ================================================================
  BLDC Motor Health Monitor — ESP32 WROOM-32
  STEP 1: DATA COLLECTION FIRMWARE
  Upload this ONLY during data collection phase
 ================================================================
  Hardware wiring:
    ISM330DHCX   Accel+Gyro     I2C Bus0  SDA=21 SCL=22  addr 0x6A
    MMC5983MA    Magnetometer   I2C Bus0  SDA=21 SCL=22  addr 0x30
    INA219       Current+Volt   I2C Bus0  SDA=21 SCL=22  addr 0x40
    OLED #1      Sensor data    I2C Bus0  SDA=21 SCL=22  addr 0x3C
    OLED #2      Status         I2C Bus1  SDA=17 SCL=16  addr 0x3C
    LM35         Temperature    GPIO34 (analog)
    IR Sensor    RPM            GPIO27 (interrupt)
    ESC 30A      Motor control  GPIO18 (PWM)

  Libraries (install via Tools -> Manage Libraries):
    SparkFun ISM330DHCX      by SparkFun Electronics
    SparkFun MMC5983MA        by SparkFun Electronics
    Adafruit INA219           by Adafruit
    Adafruit SSD1306          by Adafruit
    Adafruit GFX Library      by Adafruit

  Board: ESP32 Dev Module | Upload Speed: 921600 | Flash: 4MB
 ================================================================
*/

#include <Wire.h>
#include <SparkFun_ISM330DHCX.h>
#include <SparkFun_MMC5983MA_Arduino_Library.h>
#include <Adafruit_INA219.h>
#include <Adafruit_SSD1306.h>
#include <Adafruit_GFX.h>

// ── Pins ─────────────────────────────────────────────────────
#define SDA0     21
#define SCL0     22
#define SDA1     33
#define SCL1     32
#define LM35_PIN 34
#define IR_PIN   27
#define ESC_PIN  18

// ── ESC ──────────────────────────────────────────────────────
// ESC_CH removed — not needed in ESP32 Core v3.x
#define ESC_FREQ 50
#define ESC_RES  16
#define ESC_MIN  4915
#define ESC_SPD  6554

// ── Objects ──────────────────────────────────────────────────
SparkFun_ISM330DHCX imu;
SFE_MMC5983MA       mag;
Adafruit_INA219     ina219;
// Wire1 is now pre-defined by ESP32 Core v3.x — no TwoWire declaration needed
Adafruit_SSD1306    oled1(128, 64, &Wire,  -1);
Adafruit_SSD1306    oled2(128, 64, &Wire1, -1);
sfe_ism_data_t      accelData;

volatile unsigned long pulseCount = 0;
unsigned long lastRPMTime = 0;
float rpm = 0.0;
int   rowCount = 0;
float g_temp=0, g_ax=0, g_ay=0, g_az=0, g_cur=0, g_volt=0;
float g_mx=0, g_my=0, g_mz=0;

void IRAM_ATTR onIRPulse() {
  static unsigned long last = 0;
  unsigned long now = millis();
  if (now - last > 5) { pulseCount++; last = now; }
}

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
  float t  = mV / 10.0;
  return (t < -10 || t > 150) ? 0.0 : t;
}

void readVib(float &ax, float &ay, float &az) {
  if (imu.checkStatus()) {
    imu.getAccel(&accelData);
    ax = accelData.xData / 1000.0;
    ay = accelData.yData / 1000.0;
    az = accelData.zData / 1000.0;
  } else { ax = ay = az = 0.0; }
}

void readMag(float &mx, float &my, float &mz) {
  uint32_t rx, ry, rz;
  mag.getMeasurementXYZ(&rx, &ry, &rz);
  mx = ((float)rx - 131072.0) / 16384.0;
  my = ((float)ry - 131072.0) / 16384.0;
  mz = ((float)rz - 131072.0) / 16384.0;
}

void setup() {
  Serial.begin(115200);
  Serial.println("\n=== DATA COLLECTION MODE ===");

  Wire.begin(SDA0, SCL0);   Wire.setClock(400000);
  Wire1.begin(SDA1, SCL1);  Wire1.setClock(400000);
  delay(100);

  if (!initISM()) { Serial.println("HALT:ISM330DHCX"); while(1) delay(1000); }
  initMAG();
  if (!ina219.begin()) { Serial.println("HALT:INA219"); while(1) delay(1000); }
  ina219.setCalibration_32V_2A();
  Serial.println("INA219 OK");

  oled1.begin(SSD1306_SWITCHCAPVCC, 0x3C);
  oled1.clearDisplay(); oled1.setTextColor(WHITE); oled1.setTextSize(1);
  oled1.setCursor(0,0); oled1.println("BLDC Monitor V2"); oled1.display();

  oled2.begin(SSD1306_SWITCHCAPVCC, 0x3C);
  oled2.clearDisplay(); oled2.setTextColor(WHITE); oled2.setTextSize(1);
  oled2.setCursor(0,0); oled2.println("Data Collection"); oled2.display();

  pinMode(IR_PIN, INPUT_PULLUP);
  attachInterrupt(digitalPinToInterrupt(IR_PIN), onIRPulse, RISING);

  // ── FIX: Updated for ESP32 Arduino Core v3.x ─────────────
  // Old (Core v2.x):
  //   ledcSetup(ESC_CH, ESC_FREQ, ESC_RES);
  //   ledcAttachPin(ESC_PIN, ESC_CH);
  //   ledcWrite(ESC_CH, value);
  // New (Core v3.x): channel is auto-assigned, use pin directly
  ledcAttach(ESC_PIN, ESC_FREQ, ESC_RES);
  ledcWrite(ESC_PIN, ESC_MIN);
  // ─────────────────────────────────────────────────────────

  oled2.clearDisplay(); oled2.setCursor(0,0);
  oled2.println("Arming ESC..."); oled2.println("Please wait 3s"); oled2.display();
  delay(3000);

  for (int s = ESC_MIN; s <= ESC_SPD; s += 30) {
    ledcWrite(ESC_PIN, s); delay(50);  // FIX: pin instead of channel
  }
  Serial.println("Motor running!");

  lastRPMTime = millis();
  Serial.println("READY");
  Serial.println("Temperature,VibrationX,VibrationY,VibrationZ,Current,Voltage,RPM,MagX,MagY,MagZ");
}

void loop() {
  unsigned long now = millis();
  if (now - lastRPMTime >= 1000) {
    noInterrupts();
    unsigned long c = pulseCount; pulseCount = 0;
    interrupts();
    rpm = (c / ((now - lastRPMTime) / 1000.0)) * 60.0 / 6.0;
    lastRPMTime = now;
  }

  g_temp = readTemp();
  readVib(g_ax, g_ay, g_az);
  g_cur  = max(0.0f, ina219.getCurrent_mA() / 1000.0f);
  g_volt = ina219.getBusVoltage_V();
  readMag(g_mx, g_my, g_mz);

  Serial.printf("%.2f,%.4f,%.4f,%.4f,%.4f,%.2f,%.1f,%.3f,%.3f,%.3f\n",
    g_temp, g_ax, g_ay, g_az, g_cur, g_volt, rpm, g_mx, g_my, g_mz);

  rowCount++;

  // OLED #1 — sensor values
  oled1.clearDisplay(); oled1.setTextSize(1); oled1.setTextColor(WHITE);
  oled1.setCursor(0, 0);  oled1.printf("T:%.1fC  I:%.3fA", g_temp, g_cur);
  oled1.setCursor(0, 12); oled1.printf("V:%.1fV  RPM:%.0f", g_volt, rpm);
  oled1.setCursor(0, 24); oled1.printf("Ax:%.3f Ay:%.3f", g_ax, g_ay);
  oled1.setCursor(0, 36); oled1.printf("Az:%.3f", g_az);
  oled1.setCursor(0, 48); oled1.printf("Row #%d", rowCount);
  oled1.display();

  // OLED #2 — collection status
  oled2.clearDisplay(); oled2.setTextSize(1); oled2.setTextColor(WHITE);
  oled2.setCursor(0, 0);  oled2.println("COLLECTING...");
  oled2.drawLine(0, 10, 127, 10, WHITE);
  oled2.setCursor(0, 16); oled2.printf("Mx:%.2f My:%.2f", g_mx, g_my);
  oled2.setCursor(0, 28); oled2.printf("Mz:%.2f", g_mz);
  oled2.setCursor(0, 40); oled2.printf("VibZ:%.3f", g_az);
  oled2.setCursor(0, 52); oled2.printf("Rows: %d", rowCount);
  oled2.display();

  delay(500);
}
