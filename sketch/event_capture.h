// Logica di cattura degli eventi, senza dipendenze da Arduino/Zephyr:
// si compila anche sul PC (vedi sketch/test/test_capture.cpp).
//
// Ogni campione (piezo, luce) entra in feed(). La classe tiene un buffer
// circolare per il pre-trigger, stima baseline e rumore del piezo e, quando
// |piezo - baseline| supera la soglia adattiva (con il piezo collegato
// direttamente all'ADC la baseline e' ~0 e contano solo i picchi positivi), compone un blocco di
// BLOCK_LEN campioni con il campione di trigger all'indice PRE_TRIGGER.
#pragma once
#include <stdint.h>
#include <math.h>
#include <string.h>

struct Sample {
  uint16_t piezo;
  int16_t luce;
};

struct CaptureConfig {
  float k_noise = 6.0f;        // soglia = max(min_thr, k_noise * rumore rms)
  float min_thr = 12.0f;       // conteggi ADC a 12 bit (= 3 a 10 bit, "urto piccolo" dello sketch di prova)
  float base_alpha = 1.0f / 1024;   // baseline: costante di tempo ~0,5 s a 2 kHz
  float noise_alpha = 1.0f / 256;   // rumore: ~0,13 s
  uint32_t warmup = 2000;      // campioni prima di armare il trigger (1 s)
  int confirm = 2;             // campioni sopra soglia richiesti (1 = basta un campione)...
  int confirm_window = 4;      // ...entro questa finestra: un picco isolato di rumore non fa
                               // scattare, un urto si' anche se raddrizzato (lobi di 1 campione)
};

template <int PRE_TRIGGER, int BLOCK_LEN>
class EventCapture {
 public:
  static const int POST = BLOCK_LEN - PRE_TRIGGER;  // incluso il campione di trigger
  static const int RING = 128;                       // potenza di 2 >= PRE_TRIGGER + confirm
  static_assert(RING >= PRE_TRIGGER + 8, "ring troppo piccolo");

  explicit EventCapture(const CaptureConfig& cfg = CaptureConfig()) : cfg_(cfg) {}

  // Restituisce true quando un blocco e' completo: lo si legge con block().
  // Finche' il blocco non viene rilasciato con release(), non scattano nuovi
  // trigger (tempo morto della trasmissione), ma il buffer circolare continua
  // a riempirsi cosi' il pre-trigger del prossimo evento e' gia' pronto.
  bool feed(Sample s, bool force = false) {
    seen_++;
    float x = (float)s.piezo;
    if (seen_ == 1) base_ = x;
    float dev = x - base_;
    if (dev < 0) dev = -dev;
    last_dev_ = dev;

    bool done = false;
    if (capturing_) {
      block_[PRE_TRIGGER + post_] = s;
      if (++post_ == POST) {
        capturing_ = false;
        ready_ = true;
        done = true;
      }
    } else {
      bool above = dev > threshold();
      int need = cfg_.confirm < 1 ? 1 : cfg_.confirm;
      int window = cfg_.confirm_window < need ? need : cfg_.confirm_window;
      if (window > 8) window = 8;
      // candidato: dal primo campione sopra soglia si contano quelli sopra
      // soglia entro la finestra; se non bastano il candidato decade
      if (cand_age_ >= 0) {
        cand_age_++;
        if (above) cand_count_++;
        // fuori dalla finestra il candidato decade sempre, confermato o no:
        // cosi' back resta < window anche se il trigger non era armato
        if (cand_age_ >= window) cand_age_ = -1;
      }
      if (cand_age_ < 0 && above) {
        cand_age_ = 0;
        cand_count_ = 1;
      }
      bool confirmed = cand_age_ >= 0 && cand_count_ >= need;
      bool armed = !ready_ && seen_ > cfg_.warmup && filled_ >= PRE_TRIGGER + window;
      if (armed && (force || confirmed)) {
        // il trigger e' il PRIMO campione sopra soglia (cand_age_ campioni fa):
        // finisce all'indice PRE_TRIGGER, i successivi gia' visti vanno dopo
        int back = confirmed ? cand_age_ : 0;
        if (back < 0 || back >= 8) back = 0;  // difesa: mai fuori dal buffer
        for (int i = 0; i < PRE_TRIGGER + back; i++) {
          block_[i] = ring_[(head_ - PRE_TRIGGER - back + i) & (RING - 1)];
        }
        block_[PRE_TRIGGER + back] = s;
        post_ = 1 + back;
        capturing_ = true;
        forced_ = force && !confirmed;
        trigger_dev_ = dev;
        triggers_++;
        cand_age_ = -1;
      } else if (!above) {
        // baseline e rumore si aggiornano solo a riposo: un urto non li sporca
        base_ += cfg_.base_alpha * (x - base_);
        noise_sq_ += cfg_.noise_alpha * (dev * dev - noise_sq_);
      }
    }

    ring_[head_] = s;
    head_ = (head_ + 1) & (RING - 1);
    if (filled_ < RING) filled_++;
    return done;
  }

  // Un campione e' andato perso (scadenza mancata): un blocco in cattura
  // avrebbe una base dei tempi sbagliata, quindi si scarta.
  void abort() {
    if (capturing_) aborted_++;
    capturing_ = false;
    cand_age_ = -1;
    filled_ = 0;  // il pre-trigger deve essere di nuovo contiguo
  }

  void release() { ready_ = false; }

  const Sample* block() const { return block_; }
  bool ready() const { return ready_; }
  bool capturing() const { return capturing_; }
  bool forced() const { return forced_; }
  float threshold() const {
    float t = cfg_.k_noise * noise();
    return t > cfg_.min_thr ? t : cfg_.min_thr;
  }
  float baseline() const { return base_; }
  float noise() const { return sqrtf(noise_sq_); }
  float trigger_dev() const { return trigger_dev_; }
  uint32_t triggers() const { return triggers_; }
  uint32_t aborted() const { return aborted_; }

 private:
  CaptureConfig cfg_;
  Sample ring_[RING];
  Sample block_[BLOCK_LEN];
  int head_ = 0;
  int filled_ = 0;
  int post_ = 0;
  bool capturing_ = false;
  volatile bool ready_ = false;  // scritto dal campionatore, letto dal loop()
  bool forced_ = false;
  float base_ = 0;
  float noise_sq_ = 0;
  int cand_age_ = -1;   // campioni dal primo sopra soglia (-1 = nessun candidato)
  int cand_count_ = 0;
  float last_dev_ = 0;
  float trigger_dev_ = 0;
  uint32_t seen_ = 0;
  uint32_t triggers_ = 0;
  uint32_t aborted_ = 0;
};
