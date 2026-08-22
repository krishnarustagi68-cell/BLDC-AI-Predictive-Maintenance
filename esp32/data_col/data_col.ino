#include <Wire.h>
#include <SparkFun_ISM330DHCX.h>
#include <SparkFun_MMC5983MA_Arduino_Library.h>
#include <Adafruit_INA219.h>
#include <Adafruit_SSD1306.h>
#include <Adafruit_GFX.h>

#define SDA0 21
#define SCL0 22
#define SDA1 33
#define SCL1 32
#define LM35_PIN 34
#define IR_PIN 27
#define ESC_PIN 18

#define ESC_FREQ 50
#define ESC_RES 16
#define ESC_MIN 3277
#define ESC_SPD 6000

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

void IRAM_ATTR onIRPulse() {
  static unsigned long last = 0;
  unsigned long now = millis();
  if (now - last > 5) {
    pulseCount++;
    last = now;
  }
}

bool initISM() {
  if (!imu.begin()) return false;
  imu.deviceReset();
  delay(25);
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
  mag.softReset();
  delay(10);
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

void setup() {
  Serial.begin(115200);
  delay(500);
  Serial.println("=== DATA COLLECTION MODE ===");

  // ESC FIRST
  Serial.println("[ESC] Configuring LEDC on GPIO18...");
  ledcAttach(ESC_PIN, ESC_FREQ, ESC_RES);
  Serial.println("[ESC] Sending arming signal...");
  ledcWrite(ESC_PIN, ESC_MIN);
  Serial.println("[ESC] Waiting 5s for ESC to arm...");
  delay(5000);

  Serial.println("[ESC] Ramping up motor...");
  for (int s = ESC_MIN; s <= ESC_SPD; s += 25) {
    ledcWrite(ESC_PIN, s);
    delay(100);
  }
  Serial.println("[ESC] Motor running!");

  // I2C buses
  Wire.begin(SDA0, SCL0);
  Wire.setClock(400000);
  Wire1.begin(SDA1, SCL1);
  Wire1.setClock(400000);
  delay(100);

  // Sensors (non-blocking)
  imuOK = initISM();
  Serial.println(imuOK ? "[ISM] OK" : "[ISM] NOT FOUND");

  magOK = initMAG();
  Serial.println(magOK ? "[MAG] OK" : "[MAG] NOT FOUND");

  inaOK = ina219.begin(&Wire1);
  if (inaOK) {
    ina219.setCalibration_32V_2A();
    Serial.println("[INA] OK on Wire1");
  } else {
    Serial.println("[INA] NOT FOUND on Wire1");
  }

  // OLEDs
  if (oled1.begin(SSD1306_SWITCHCAPVCC, 0x3C)) {
    oled1.clearDisplay();
    oled1.setTextColor(WHITE);
    oled1.setTextSize(1);
    oled1.setCursor(0, 0);
    oled1.println("BLDC Data Coll.");
    oled1.display();
  }

  if (oled2.begin(SSD1306_SWITCHCAPVCC, 0x3C)) {
    oled2.clearDisplay();
    oled2.setTextColor(WHITE);
    oled2.setTextSize(1);
    oled2.setCursor(0, 0);
    oled2.println("Motor Running!");
    oled2.display();
  }

  // IR sensor
  pinMode(IR_PIN, INPUT_PULLUP);
  attachInterrupt(digitalPinToInterrupt(IR_PIN), onIRPulse, RISING);
  Serial.println("[IR] Interrupt attached on GPIO27");

  lastRPMTime = millis();

  Serial.println("=== READY ===");
  Serial.println("Temperature,VibrationX,VibrationY,VibrationZ,Current,Voltage,RPM,MagX,MagY,MagZ");
}

void loop() {
  unsigned long now = millis();

  if (now - lastRPMTime >= 1000) {
    noInterrupts();
    unsigned long c = pulseCount;
    pulseCount = 0;
    interrupts();
    rpm = (c / ((now - lastRPMTime) / 1000.0)) * 60.0 / 6.0;
    lastRPMTime = now;
  }

  g_temp = readTemp();
  readVib(g_ax, g_ay, g_az);

  if (inaOK) {
    g_cur = max(0.0f, ina219.getCurrent_mA() / 1000.0f);
    g_volt = ina219.getBusVoltage_V();
  } else {
    g_cur = 0.0f;
    g_volt = 0.0f;
  }

  readMag(g_mx, g_my, g_mz);

  Serial.printf("%.2f,%.4f,%.4f,%.4f,%.4f,%.2f,%.1f,%.3f,%.3f,%.3f\n",
    g_temp, g_ax, g_ay, g_az, g_cur, g_volt, rpm, g_mx, g_my, g_mz);
  rowCount++;

  oled1.clearDisplay();
  oled1.setTextSize(1);
  oled1.setTextColor(WHITE);
  oled1.setCursor(0, 0);
  oled1.printf("T:%.1fC I:%.3fA", g_temp, g_cur);
  oled1.setCursor(0, 12);
  oled1.printf("V:%.1fV RPM:%.0f", g_volt, rpm);
  oled1.setCursor(0, 24);
  oled1.printf("Ax:%.3f Ay:%.3f", g_ax, g_ay);
  oled1.setCursor(0, 36);
  oled1.printf("Az:%.3f", g_az);
  oled1.setCursor(0, 48);
  oled1.printf("Row #%d", rowCount);
  oled1.display();

  oled2.clearDisplay();
  oled2.setTextSize(1);
  oled2.setTextColor(WHITE);
  oled2.setCursor(0, 0);
  oled2.println("COLLECTING...");
  oled2.drawLine(0, 10, 127, 10, WHITE);
  oled2.setCursor(0, 16);
  oled2.printf("Mx:%.2f My:%.2f", g_mx, g_my);
  oled2.setCursor(0, 28);
  oled2.printf("Mz:%.2f", g_mz);
  oled2.setCursor(0, 40);
  oled2.printf("VibZ:%.3f", g_az);
  oled2.setCursor(0, 52);
  oled2.printf("Rows: %d", rowCount);
  oled2.display();

  delay(500);
}
