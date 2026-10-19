#include <Arduino.h>

void setup() {
  Serial.begin(115200);
  delay(300);
  Serial.println();
  Serial.println("BINDDECKLITE DIAG BOOT OK");
  Serial.printf("cores=%d flash=%u\n", (int)ESP.getChipCores(), (unsigned)ESP.getFlashChipSize());
}

void loop() {
  Serial.println("DIAG ALIVE");
  delay(1500);
}
