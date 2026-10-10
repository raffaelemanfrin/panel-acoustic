// Simulatore su PC della cattura: legge "piezo,luce" (un campione per riga a
// 2000 Hz) da stdin e stampa i blocchi esattamente come send_block() nello
// sketch. Serve a provare trigger, allineamento e protocollo senza scheda:
//   g++ -std=c++17 -O2 -I.. capture_sim.cpp -o capture_sim
//   python3 ../../python/stream_sim.py | ./capture_sim | python3 ../../python/daemon.py --input -
#include <cstdio>
#include <cstdlib>
#include "event_capture.h"

int main(int argc, char** argv) {
  EventCapture<100, 500> cap;
  unsigned p;
  int l;
  long n = 0;
  int force_every = argc > 1 ? atoi(argv[1]) : 0;  // blocco forzato ogni N campioni
  while (scanf("%u,%d", &p, &l) == 2) {
    n++;
    bool force = force_every && n % force_every == 0;
    Sample s{(uint16_t)p, (int16_t)l};
    if (cap.feed(s, force)) {
      const Sample* b = cap.block();
      if (cap.forced()) printf("#FORCED\n");
      printf("---EVENT_START---\n");
      for (int i = 0; i < 500; i++) printf("%d,%d,%d\n", i, (int)b[i].piezo, (int)b[i].luce);
      printf("---EVENT_END---\n");
      fflush(stdout);
      cap.release();  // sul PC la trasmissione e' istantanea
    }
  }
  fprintf(stderr, "campioni %ld, trigger %u, soglia finale %.1f, rumore %.2f, baseline %.1f\n", n,
          cap.triggers(), cap.threshold(), cap.noise(), cap.baseline());
  return 0;
}
