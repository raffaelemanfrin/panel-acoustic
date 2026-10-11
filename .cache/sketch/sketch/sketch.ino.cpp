#include <Arduino.h>
#line 1 "/home/arduino/ArduinoApps/panel-acoustic/sketch/sketch.ino"
// Solar-Acoustics: firmware MCU della UNO Q (versione semplice, circuito finale)
//
// Circuito (relazione elettronica):
//   A0 = piezo collegato direttamente (niente partitore ne' condensatore):
//        a riposo ~0, l'urto da' solo picchi positivi (le semionde negative si perdono)
//   A1 = tensione del pannello attraverso il partitore 100k / 10k
//        (5,3 V a vuoto -> ~0,48 V sul pin, ~600 conteggi a 12 bit)
//
// Protocollo MCU -> Linux (convenzioni del team):
//   - 2000 Hz (un campione ogni 500 us), ADC a 12 bit (0-4095)
//   - buffer circolare per il pre-trigger; blocco di 500 campioni con il
//     campione di trigger SEMPRE all'indice 100 (50 ms prima, 200 ms dopo)
//   - ---EVENT_START--- / "indice,piezo,luce" (indice 0..499) / ---EVENT_END---
//
// Tutto gira nel loop(), come lo sketch di prova: niente thread, niente I2C.
// I blocchi vanno a Linux come notifiche Bridge "pa_lines" (sulla UNO Q
// 'Serial' e' il Monitor di App Lab e non arriva a Python). Durante l'invio
// (~0,5 s) non si campiona: e' il tempo morto previsto dal protocollo.
//
// Blocchi forzati (per registrare calma e vento, che non fanno scattare il
// trigger): ponticello tra D2 e GND -> un blocco al secondo, preceduto dalla
// riga "#FORCED". Anche Linux puo' chiederli con Bridge "pa_force" (N blocchi).
//
// DEBUG_RAW 1: niente blocchi, stampa sul Monitor picco del piezo e luce ogni
// 100 ms, come lo sketch di prova. Serve a controllare i sensori e a scegliere
// TRIG_MIN guardando i valori a riposo e con un colpo leggero.

#include <Arduino_RouterBridge.h>

#include "event_capture.h"

// ------------------------------------------------------------ configurazione
#define DEBUG_RAW       0           // 1 = solo valori grezzi sul Monitor (verifica sensori)

#define ADC_BITS        12          // Linux: --adc-bits 12
#define SAMPLE_US       1000         // 2000 Hz
#define LIGHT_EVERY 10
#define MAX_LAG_US 4000
#define PRE_TRIGGER     100
#define BLOCK_LEN       500
#define PIEZO_PIN       A0
#define LIGHT_PIN       A1

// Trigger: piezo - baseline > max(TRIG_MIN, TRIG_K x rumore medio).
// Lo sketch di prova chiamava "urto piccolo" un picco > 3 a 10 bit = 12 a 12 bit.
#define TRIG_MIN        40          // conteggi a 12 bit
#define TRIG_K          0.0f

#define FORCE_PIN       2           // a GND = blocchi forzati; -1 per disabilitarlo
#define FORCED_GAP_MS   1000
#define LATENCY_PIN     7           // alto dal trigger a fine invio (oscilloscopio)

#define LINES_METHOD    "pa_lines"
#define CHUNK_BYTES     400
#define STATS_EVERY_MS  10000

// -------------------------------------------------------------- stato
#line 58 "/home/arduino/ArduinoApps/panel-acoustic/sketch/sketch.ino"
static CaptureConfig make_config();
#line 74 "/home/arduino/ArduinoApps/panel-acoustic/sketch/sketch.ino"
static int pa_force(int n);
#line 82 "/home/arduino/ArduinoApps/panel-acoustic/sketch/sketch.ino"
static void flush_chunk(String& chunk);
#line 89 "/home/arduino/ArduinoApps/panel-acoustic/sketch/sketch.ino"
static void send_block();
#line 112 "/home/arduino/ArduinoApps/panel-acoustic/sketch/sketch.ino"
static void print_stats();
#line 133 "/home/arduino/ArduinoApps/panel-acoustic/sketch/sketch.ino"
void setup();
#line 174 "/home/arduino/ArduinoApps/panel-acoustic/sketch/sketch.ino"
void loop();
#line 58 "/home/arduino/ArduinoApps/panel-acoustic/sketch/sketch.ino"
static CaptureConfig make_config() {
  CaptureConfig c;
  c.min_thr = TRIG_MIN;
  c.k_noise = TRIG_K;
  c.confirm=1;
  return c;
}
static EventCapture<PRE_TRIGGER, BLOCK_LEN> capture(make_config());

static volatile uint32_t g_force_pending = 0;  // blocchi forzati chiesti da Linux
static uint32_t g_next_us = 0;                 // prossimo istante di campionamento
static uint32_t g_last_block_ms = 0;
static uint32_t g_samples = 0, g_overruns = 0, g_sent = 0;
static bool g_force_armed = false;             // il prossimo campione e' un trigger forzato

// Chiamata da Linux (capture.py --force N): N blocchi forzati, uno al secondo.
static int pa_force(int n) {
  if (n < 0) n = 0;
  if (n > 1000) n = 1000;
  g_force_pending = (uint32_t)n;
  return n;
}

// -------------------------------------------------------------- invio
static void flush_chunk(String& chunk) {
  if (chunk.length()) {
    Bridge.notify(LINES_METHOD, chunk);
    chunk = "";
  }
}

static void send_block() {
  const Sample* b = capture.block();
  String chunk;
  chunk.reserve(CHUNK_BYTES + 32);
  if (capture.forced()) chunk += "#FORCED\n";
  chunk += "---EVENT_START---\n";
  for (int i = 0; i < BLOCK_LEN; i++) {
    chunk += i;
    chunk += ',';
    chunk += (int)b[i].piezo;
    chunk += ',';
    chunk += (int)b[i].luce;
    chunk += '\n';
    if (chunk.length() >= CHUNK_BYTES) flush_chunk(chunk);
  }
  chunk += "---EVENT_END---\n";
  flush_chunk(chunk);
  g_sent++;
  g_last_block_ms = millis();
  digitalWrite(LATENCY_PIN, LOW);
  capture.release();
}

static void print_stats() {
  Monitor.print("campioni/s ");
  Monitor.print((unsigned long)(g_samples / (STATS_EVERY_MS / 1000)));
  Monitor.print(" | baseline ");
  Monitor.print((int)capture.baseline());
  Monitor.print(" rumore ");
  Monitor.print(capture.noise(), 1);
  Monitor.print(" soglia ");
  Monitor.print((int)capture.threshold());
  Monitor.print(" | trigger ");
  Monitor.print((unsigned long)capture.triggers());
  Monitor.print(" inviati ");
  Monitor.print((unsigned long)g_sent);
  Monitor.print(" ritardi ");
  Monitor.print((unsigned long)g_overruns);
  Monitor.print(" | luce ");
  Monitor.println(analogRead(LIGHT_PIN));
  g_samples = 0;
}

// -------------------------------------------------------------- setup
void setup() {
  analogReadResolution(ADC_BITS);
  pinMode(LATENCY_PIN, OUTPUT);
  digitalWrite(LATENCY_PIN, LOW);
  if (FORCE_PIN >= 0) pinMode(FORCE_PIN, INPUT_PULLUP);

  Bridge.begin();
  Monitor.begin();
#if !DEBUG_RAW
  Bridge.provide("pa_force", pa_force);
#endif

  Monitor.print("Solar-Acoustics MCU: ");
  Monitor.print(DEBUG_RAW ? "DEBUG_RAW (solo valori grezzi)" : "2000 Hz, blocchi via Bridge");
  Monitor.print(", ADC ");
  Monitor.print(ADC_BITS);
  Monitor.println(" bit");
  g_next_us = micros();
}

// -------------------------------------------------------------- loop
#if DEBUG_RAW

void loop() {
  // come lo sketch di prova, ma senza delay: il piezo si legge di continuo
  static int picco = 0;
  static uint32_t t0 = 0;
  int v = analogRead(PIEZO_PIN);
  if (v > picco) picco = v;
  if (millis() - t0 >= 100) {
    t0 = millis();
    Monitor.print("piezo max ");
    Monitor.print(picco);
    Monitor.print("  luce ");
    Monitor.println(analogRead(LIGHT_PIN));
    picco = 0;
  }
}

#else

void loop() {
  // attesa attiva fino al prossimo istante di campionamento (500 us)
  
  while ((int32_t)(micros() - g_next_us) < 0) {
  }
  g_next_us += SAMPLE_US;

  static int16_t luce = 0;
  static uint8_t light_count = 0;
  Sample s;
  s.piezo = (uint16_t)analogRead(PIEZO_PIN);
  if (light_count == 0) luce = (int16_t)analogRead(LIGHT_PIN);
  light_count = (light_count + 1) % LIGHT_EVERY;
  s.luce = luce;
  g_samples++;

  bool was_capturing = capture.capturing();
  if (capture.feed(s, g_force_armed)) {
    send_block();               // ~0,5 s: tempo morto
    g_next_us = micros();       // si riparte da adesso
    return;
  }
  if (!was_capturing && capture.capturing()) {
    digitalWrite(LATENCY_PIN, HIGH);
    g_force_armed = false;      // il forzato (o un trigger vero) e' partito
  }

  // in ritardo di piu' di un periodo: base dei tempi persa, il blocco in corso si scarta
  if ((int32_t)(micros() - g_next_us) > MAX_LAG_US) {
    g_overruns++;
    g_next_us = micros();
  }

  if (capture.capturing()) return;  // durante la cattura nient'altro

  // blocchi forzati: ponticello D2-GND o richiesta da Linux, uno al secondo
  bool want = g_force_pending > 0 || (FORCE_PIN >= 0 && digitalRead(FORCE_PIN) == LOW);
  if (want && !g_force_armed && millis() - g_last_block_ms >= FORCED_GAP_MS) {
    if (g_force_pending > 0) g_force_pending--;
    g_force_armed = true;
  }

  static uint32_t last_stats = 0;
  if (millis() - last_stats >= STATS_EVERY_MS) {
    last_stats = millis();
    print_stats();
    capture.abort();            // la stampa salta qualche campione: pre-trigger da rifare
    g_next_us = micros();
  }

}

#endif

