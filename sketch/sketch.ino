// Solar-Acoustics: firmware MCU (STM32U585 della UNO Q)
//
// Protocollo MCU -> Linux (convenzioni del team):
//   - piezo campionato a 2000 Hz (un campione ogni 500 us), ADC a 12 bit (0-4095)
//   - buffer circolare per il pre-trigger; al trigger si raccolgono i campioni
//     successivi e si invia il blocco intero quando e' completo
//   - blocco: ---EVENT_START---, 500 righe "indice,piezo,luce" con indice 0..499,
//     ---EVENT_END---; il campione di trigger e' SEMPRE l'indice 100
//     (100 pre = 50 ms, 400 post compreso il trigger = 200 ms)
//   - luce = tensione di shunt dell'INA219 (registro 0x01, 1 LSB = 10 uV,
//     proporzionale alla corrente del pannello), letta nel loop() e mai nel
//     campionamento del piezo
//
// Trasporto sulla UNO Q: l'UART interno MCU<->Linux e' di arduino-router, e
// 'Serial' nello sketch e' il Monitor di App Lab (non arriva a Python).
// I blocchi viaggiano quindi come notifiche Bridge "pa_lines" (testo del
// protocollo, a pezzi da ~400 byte); lato Linux li riceve source.bridge_lines()
// e il parser e' lo stesso di sempre. Collegamento a 115200 baud (default di
// arduino-router): un blocco (~6 kB) richiede ~0,5 s di tempo morto.
//
// Perche' un thread e non un interrupt: analogRead() sulla UNO Q passa dal
// driver ADC di Zephyr, che si blocca su un semaforo e non si puo' chiamare in
// una ISR. Il piezo e' campionato da un thread cooperativo ad alta priorita'
// che dorme fino a poco prima della scadenza e poi aspetta il microsecondo
// giusto; il loop() (INA219, invio) gira nei ~400 us liberi tra due campioni.
//
// Modalita' forzata (per registrare calma e vento, che non fanno scattare il
// trigger): Linux chiama Bridge "pa_force" con N -> la MCU invia N blocchi
// senza trigger, uno al secondo, preceduti dalla riga "#FORCED" (ignorata dal
// parser). Da Linux: python capture.py --bridge --label calma --force 30
// In alternativa: pin FORCE_PIN a GND = un blocco forzato al secondo finche' resta a GND.

#include <Arduino_RouterBridge.h>
#include <Wire.h>

#include "event_capture.h"

// ------------------------------------------------------------ configurazione
#define ADC_BITS        12          // comunicato a Linux: --adc-bits 12
#define SAMPLE_US       500         // 2000 Hz
#define PRE_TRIGGER     100
#define BLOCK_LEN       500
#define PIEZO_PIN       A0

#define LIGHT_INA219    10          // 1 = INA219 su I2C; 0 = partitore su LIGHT_PIN (prototipo)
#define LIGHT_PIN       A1
#define INA219_ADDR     0x40
#define LIGHT_PERIOD_US 1000        // lettura luce ~1 kHz (una conversione INA219 a 12 bit = 532 us)

#define LATENCY_PIN     7           // alto al trigger, basso a fine invio (oscilloscopio)
#define FORCE_PIN       2           // a GND = blocchi forzati; -1 per disabilitarlo
#define FORCED_GAP_MS   1000

#define BRIDGE_BAUD     115200      // deve coincidere con arduino-router (-b); vedi README
#define LINES_METHOD    "pa_lines"
#define CHUNK_BYTES     400
#define STATS_EVERY_MS  10000

// -------------------------------------------------------------- stato globale
static EventCapture<PRE_TRIGGER, BLOCK_LEN> capture;
static volatile int16_t g_light = 0;          // ultimo valore di luce (scritto dal loop)
static volatile uint32_t g_force_pending = 0; // blocchi forzati richiesti da Linux
static volatile uint32_t g_overruns = 0;      // scadenze di campionamento mancate
static volatile uint32_t g_samples = 0;
static volatile bool g_force_now = false;     // richiesta per il campionatore
static uint32_t g_last_block_ms = 0;
static uint32_t g_sent = 0;
static uint32_t g_light_errors = 0;
static int32_t g_sleep_guard_us = 0;

static k_tid_t sampler_tid;
static struct k_thread sampler_thread;
static k_thread_stack_t* sampler_stack;

// ------------------------------------------------------------ campionamento
static void sampler_entry(void*, void*, void*) {
  uint32_t next = micros() + SAMPLE_US;
  for (;;) {
    // dorme fino a poco prima della scadenza, poi attesa attiva fino al microsecondo
    int32_t rem = (int32_t)(next - micros());
    if (rem > g_sleep_guard_us) {
      k_sleep(K_USEC(rem - g_sleep_guard_us));
    }
    while ((int32_t)(next - micros()) > 0) {
    }

    Sample s;
    s.piezo = (uint16_t)analogRead(PIEZO_PIN);
    s.luce = g_light;
    g_samples++;

    bool force = false;
    if (g_force_now && !capture.capturing() && !capture.ready()) {
      force = true;
      g_force_now = false;
    }
    bool was_capturing = capture.capturing();
    capture.feed(s, force);
    if (!was_capturing && capture.capturing()) {
      digitalWrite(LATENCY_PIN, HIGH);
    } else if (force) {
      g_force_now = true;  // trigger non ancora armato (avvio): riprova al prossimo campione
    }

    next += SAMPLE_US;
    if ((int32_t)(micros() - next) > 0) {
      // in ritardo di oltre un periodo: base dei tempi persa
      g_overruns++;
      capture.abort();
      next = micros() + SAMPLE_US;
    }
  }
}

// -------------------------------------------------------------------- luce
#if LIGHT_INA219
static void ina219_init() {
  Wire.begin();
  Wire.setClock(400000);
  // configurazione: 32 V, PGA /8 (+-320 mV), ADC 12 bit (532 us), shunt+bus continuo
  Wire.beginTransmission(INA219_ADDR);
  Wire.write(0x00);
  Wire.write(0x39);
  Wire.write(0x9F);
  if (Wire.endTransmission() != 0) {
    Monitor.println("ATTENZIONE: INA219 non risponde su I2C (indirizzo 0x40?)");
  }
}

static bool read_light(int16_t* out) {
  Wire.beginTransmission(INA219_ADDR);
  Wire.write(0x01);  // tensione di shunt, 10 uV/LSB, con segno
  if (Wire.endTransmission(false) != 0) return false;
  if (Wire.requestFrom(INA219_ADDR, 2) != 2) return false;
  uint16_t hi = Wire.read();
  uint16_t lo = Wire.read();
  *out = (int16_t)((hi << 8) | lo);
  return true;
}
#else
static void ina219_init() {}
static bool read_light(int16_t* out) {
  *out = (int16_t)analogRead(LIGHT_PIN);
  return true;
}
#endif

// ------------------------------------------------------------------- invio
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
  capture.release();  // da qui il trigger e' di nuovo armato
}

// Chiamata da Linux: pa_force(n) -> n blocchi forzati, uno al secondo
static int pa_force(int n) {
  if (n < 0) n = 0;
  if (n > 1000) n = 1000;
  g_force_pending = (uint32_t)n;
  return n;
}

// ------------------------------------------------------------------- setup
void setup() {
  pinMode(LATENCY_PIN, OUTPUT);
  digitalWrite(LATENCY_PIN, LOW);
  if (FORCE_PIN >= 0) pinMode(FORCE_PIN, INPUT_PULLUP);
  analogReadResolution(ADC_BITS);

  Bridge.begin(BRIDGE_BAUD);
  Monitor.begin();
  Bridge.provide("pa_force", pa_force);

  ina219_init();
  int16_t l;
  if (read_light(&l)) g_light = l;

  // il sonno del kernel ha la granularita' del tick: si sveglia il thread un
  // tick (piu' margine) prima della scadenza e si aspetta il resto
  int32_t tick_us = 1000000 / CONFIG_SYS_CLOCK_TICKS_PER_SEC;
  g_sleep_guard_us = tick_us + 80;
  if (g_sleep_guard_us >= SAMPLE_US) {
    Monitor.println("ATTENZIONE: tick del kernel troppo lento, campionamento in attesa attiva");
  }

  // thread cooperativo: non viene interrotto da altri thread mentre campiona
  sampler_stack = k_thread_stack_alloc(1024, 0);
  sampler_tid = k_thread_create(&sampler_thread, sampler_stack, 1024, sampler_entry,
                                NULL, NULL, NULL, K_PRIO_COOP(2), 0, K_NO_WAIT);
  k_thread_name_set(sampler_tid, "piezo");

  Monitor.print("Solar-Acoustics MCU pronta: 2000 Hz, ADC ");
  Monitor.print(ADC_BITS);
  Monitor.print(" bit, tick ");
  Monitor.print((long)tick_us);
  Monitor.println(" us");
}

// -------------------------------------------------------------------- loop
void loop() {
  static uint32_t last_light_us = 0;
  static uint32_t last_stats_ms = 0;

  uint32_t now_us = micros();
  if ((uint32_t)(now_us - last_light_us) >= LIGHT_PERIOD_US) {
    last_light_us = now_us;
    int16_t l;
    if (read_light(&l)) {
      g_light = l;
    } else {
      g_light_errors++;
    }
  }

  if (capture.ready()) {
    send_block();
  }

  // blocchi forzati: da Linux (pa_force) o dal pin
  bool want_force = g_force_pending > 0 || (FORCE_PIN >= 0 && digitalRead(FORCE_PIN) == LOW);
  if (want_force && !g_force_now && !capture.capturing() && !capture.ready() &&
      millis() - g_last_block_ms >= FORCED_GAP_MS) {
    if (g_force_pending > 0) g_force_pending--;
    g_force_now = true;
  }

  if (millis() - last_stats_ms >= STATS_EVERY_MS) {
    last_stats_ms = millis();
    Monitor.print("campioni/s ");
    Monitor.print((unsigned long)(g_samples / (STATS_EVERY_MS / 1000)));
    g_samples = 0;
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
    Monitor.print(" scartati ");
    Monitor.print((unsigned long)capture.aborted());
    Monitor.print(" | ritardi ");
    Monitor.print((unsigned long)g_overruns);
    Monitor.print(" errori luce ");
    Monitor.print((unsigned long)g_light_errors);
    Monitor.print(" luce ");
    Monitor.println((int)g_light);
  }

  delay(1);  // cede la CPU: senza, il thread del Bridge (priorita' piu' bassa) non riceve pa_force
}
