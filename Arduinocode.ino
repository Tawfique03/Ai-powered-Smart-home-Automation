#include <Wire.h>
#include <LiquidCrystal_I2C.h>
#include <DHT.h>

LiquidCrystal_I2C lcd(0x27, 16, 2);
#define LED_PIN 2
#define BUZZER_PIN 3        
#define PIR_PIN 4
#define MQ2_PIN 5           
#define DHT_PIN 8
#define DHTTYPE DHT11
DHT dht(DHT_PIN, DHTTYPE);
#define FAN_EN 9
#define FAN_IN1 10
#define FAN_IN2 11

// ---------------- States ----------------
bool led_state = false;
bool pir_last_state = LOW;    
bool led_mode_manual = false; 
bool fan_mode_manual = false;   
int fan_manual_speed = 0;   

unsigned long lastSend = 0;
const unsigned long sendInterval = 1000;

// ---------------- Comfort Fan Speed ----------------
int calculateFanSpeed(float t, float h) {
  float discomfort = t + (0.1 * h);
  if (discomfort < 28) return 0;
  // Auto speed will be between 100 and 255
  return constrain(map(discomfort, 28, 40, 100, 255), 100, 255);
}

// ---------------- Setup ----------------
void setup() {
  pinMode(LED_PIN, OUTPUT);
  pinMode(BUZZER_PIN, OUTPUT);
  pinMode(PIR_PIN, INPUT);
  pinMode(MQ2_PIN, INPUT);

  pinMode(FAN_EN, OUTPUT);
  pinMode(FAN_IN1, OUTPUT);
  pinMode(FAN_IN2, OUTPUT);

  // Fan direction
  digitalWrite(FAN_IN1, HIGH);
  digitalWrite(FAN_IN2, LOW);

  digitalWrite(BUZZER_PIN, HIGH);

  Serial.begin(9600);
  dht.begin();

  lcd.init();
  lcd.backlight();
  lcd.clear();
  lcd.setCursor(0, 0);
  lcd.print("AI Home System");
  delay(1200);
  lcd.clear();
}

void loop() {
  float t = dht.readTemperature();
  float h = dht.readHumidity();
  bool pir = digitalRead(PIR_PIN);

  bool smokeDetected = (digitalRead(MQ2_PIN) == LOW);

  if (isnan(t) || isnan(h)) return;


  // ----------- SMOKE -----------
  if (smokeDetected) {
    tone(BUZZER_PIN, 1000);
    digitalWrite(LED_PIN, HIGH);
    analogWrite(FAN_EN, 0);
    led_state = true; 
    lcd.setCursor(0, 0);
    lcd.print("  SMOKE ALERT! ");
    lcd.setCursor(0, 1);
    lcd.print("Ventilating...   ");
    return;
  }
  else {
    noTone(BUZZER_PIN);
    digitalWrite(BUZZER_PIN, HIGH);
  }

  // ----------- SERIAL COMMAND HANDLER -----------
  if (Serial.available()) {
    String cmd = Serial.readStringUntil('\n');
    cmd.trim();

    if (cmd == "LED_ON") {
      led_mode_manual = true;
      led_state = true;
      digitalWrite(LED_PIN, HIGH);
    }
    else if (cmd == "LED_OFF") {
      led_mode_manual = true;
      led_state = false;
      digitalWrite(LED_PIN, LOW);
    }
    else if (cmd == "LED_AUTO") {
      led_mode_manual = false;
      pir_last_state = digitalRead(PIR_PIN); 
    }
    else if (cmd == "FAN_ON") {
      fan_mode_manual = true;
      fan_manual_speed = 255;
    }
    else if (cmd == "FAN_OFF") {
      fan_mode_manual = true;
      fan_manual_speed = 0;
    }
    else if (cmd == "FAN_AUTO") {
      fan_mode_manual = false;
    }
    
    else if (cmd.startsWith("FAN_PWM:")) {
      fan_mode_manual = true;
      int pwm_val = cmd.substring(8).toInt();
      fan_manual_speed = constrain(pwm_val, 0, 255);
    }
  }


  // ----------- AUTO LOGIC-----------
  if (!led_mode_manual) {
    if (pir == HIGH && pir_last_state == LOW) {
      led_state = !led_state;
      digitalWrite(LED_PIN, led_state);
    }
    pir_last_state = pir;
  }


  // ----------- FAN Control -----------
  int speed = 0; 

  if (fan_mode_manual) {
    speed = fan_manual_speed;
  }
  else {
    if (led_state) {
      speed = calculateFanSpeed(t, h);
    } else {
      speed = 0;
    }
  }
  
  analogWrite(FAN_EN, speed);


  // ----------- LCD Update -----------
  lcd.setCursor(0, 0);
  lcd.print("T:");
  lcd.print(t, 1);
  lcd.print(" H:");
  lcd.print(h, 0);
  lcd.print("%   ");

  lcd.setCursor(0, 1);
  lcd.print(led_state ? "LED:ON " : "LED:OFF");
  lcd.print(fan_mode_manual ? " Man:" : " Auto:"); 
  lcd.print(speed);
  lcd.print("   "); 

  // ----------- Serial JSON Output -----------
  if (millis() - lastSend >= sendInterval) {
    lastSend = millis();
    Serial.print("{\"temp\":");
    Serial.print(t, 1);
    Serial.print(",\"hum\":");
    Serial.print(h, 0);
    Serial.print(",\"pir\":");
    Serial.print(pir);
    Serial.print(",\"smoke\":");
    Serial.print(smokeDetected ? 1 : 0);
    Serial.print(",\"led\":");
    Serial.print(led_state);
    Serial.print(",\"fan\":");
    Serial.print(speed);
    Serial.println("}");
  }

  delay(80);
}
