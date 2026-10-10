int picco = 0;
unsigned long tStampa = 0;

void setup() {
  Serial.begin(115200);
}

void loop() {
  int v = analogRead(A0);
  if (v > picco) picco = v;        // tiene il valore massimo

  if (millis() - tStampa >= 10) {   // ogni 5 ms stampa il picco
    tStampa = millis();
    if(picco > 3 && picco < 15){
      Serial.println("Urto Piccolo:");
      Serial.println(picco);
    } else if(picco >= 15){
      Serial.println("Urto Medio:");
      Serial.println(picco);
    }

    picco = 0;
    delay(90);
  }
}