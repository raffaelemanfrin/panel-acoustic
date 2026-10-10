# 😀 panel acoustic






## Persona 3: logica, demone, uscite

Tutto in `python/`, solo libreria standard (più `pyserial` per la seriale).

| File | Cosa fa |
|---|---|
| `detector.py` | regole a soglia **solo sul piezo** (piano B, stessa firma della rete) + `light_flags()` |
| `logic.py` | logica booleana `nn_alarm`, `light_fast`, `light_slow` → allarme / avviso, mappa di Karnaugh |
| `daemon.py` | seriale → parser → classificatore → logica → SQLite → uscite |
| `outputs.py` | sirena sul jack del Media Carrier (PipeWire) + LED della UNO Q |
| `dashboard.py` + `dashboard.html` | grafico in tempo reale dal registro SQLite, porta 8080, funziona offline |
| `evaluate.py` | matrice di confusione + esito allarme su CSV etichettati (`--data`) o finti (`--fake`) |

Logica decisa dal team (A = nn_alarm, F = light_fast, S = light_slow):

```
        FS = 00  01  11  10
   A = 0      0   0   0   0        Y (allarme) = A
   A = 1      1   1   1   1        W (avviso)  = A'S
```

Un colpo è allarme con o senza ombra; il calo lento (neve/sporco) è solo avviso.
Ombra senza urto: di default non è allarme; con `--shadow-alarm` diventa Y = A + F.
`python logic.py` stampa tabella e mappe.

Sirena: `on_alarm` restituisce la durata dell'uscita e il demone scarta i blocchi
che iniziano durante la sirena più una guardia (`--siren-s 3 --guard 1`), così la
sirena non fa scattare il piezo.

```bash
cd python
python3 -m unittest testprotocol testpersona3 testdaemon testlogic
python3 logic.py
# demo senza scheda (due terminali)
python3 fakemcu.py --n 500 --order random --interval 0.5 | python3 daemon.py --input - --adc-bits 10 --siren silent
python3 dashboard.py          # http://<ip-uno-q>:8080
# con la scheda (o semplicemente Run in App Lab: main.py avvia demone + dashboard)
python3 daemon.py --bridge                          # sirena attiva di default
python3 capture.py --bridge --label grandine --n 30
python3 capture.py --bridge --label calma --force 30
python3 evaluate.py --data data/testing --adc-bits 12 --triggers-per-hour 40
```

## Firmware MCU (`sketch/`)

| Domanda | Risposta |
|---|---|
| Porta e baud lato Linux | **Nessuna tty**: sulla UNO Q l'UART interno MCU↔Linux è di `arduino-router` (115200 baud, il suo default). I blocchi arrivano come notifiche Bridge `pa_lines` sul socket `/var/run/arduino-router.sock` → `daemon.py --bridge`, `capture.py --bridge` |
| Prima colonna | indice 0–499 → `--time-col index` (default) |
| Bit ADC | 12 (0–4095) → `--adc-bits 12` (default di daemon e capture) |
| Modalità forzata (calma, vento) | `capture.py --bridge --label calma --force 30`: Linux chiama `pa_force(30)` e la MCU invia 30 blocchi senza trigger, uno al secondo (riga `#FORCED` prima di ogni blocco, ignorata dal parser). In alternativa pin D2 a GND |

- Piezo campionato a 2000 Hz da un thread cooperativo ad alta priorità (non da una ISR:
  `analogRead` sulla UNO Q usa il driver ADC di Zephyr, che non si può chiamare in interrupt).
- Trigger adattivo: |piezo − baseline| > max(40, 6 × rumore medio); baseline e rumore
  si aggiornano solo a riposo. Funziona con piezo polarizzato a metà scala o riferito a GND.
- Luce = tensione di shunt dell'INA219 (registro 0x01, 10 µV/LSB), letta nel `loop()` a ~1 kHz
  e campionata-e-tenuta in ogni riga. `LIGHT_INA219 0` usa invece `analogRead(A1)`.
- Blocco: trigger all'indice 100, 100 + 400 campioni; tempo morto ~0,5 s per l'invio.
- D7 alto dal trigger alla fine dell'invio (latenza all'oscilloscopio).
- Ogni 10 s il Monitor di App Lab mostra campioni/s, baseline, rumore, soglia, trigger,
  ritardi di campionamento ed errori I²C: è il primo controllo da fare sulla scheda.
- 230400 baud: richiede di avviare `arduino-router` con `-b 230400` e `BRIDGE_BAUD 230400`
  nello sketch; con 115200 il tempo morto (~0,5 s) è già nei limiti concordati.
- Prova senza scheda: `sketch/test/` (simulatore C++ della cattura + flusso finto a 12 bit).

## Raccolta dati e rete neurale

**Raccolta in App Lab** (`raccolta.py`): se esiste `python/raccolta.json`, Run avvia la
raccolta invece del demone (il canale Bridge è di un solo processo alla volta).
In SSH, mentre l'app gira:

```bash
cd /home/arduino/ArduinoApps/panel-acoustic/python
echo '{"label": "grandine", "n": 30}' > raccolta.json                      # eventi da trigger
echo '{"label": "calma", "n": 30, "force": true}' > raccolta.json           # blocchi forzati
echo '{"label": "colpo", "n": 15, "category": "testing"}' > raccolta.json  # sessione di test
cat raccolta_stato.txt                                                      # avanzamento
```

Con `force` si salvano solo i blocchi forzati, senza solo quelli da trigger: un evento vero
durante una raccolta di "calma" non finisce nella classe sbagliata.
CSV in `python/data/<category>/`; sul PC: `scp -r arduino@<ip>:/home/arduino/ArduinoApps/panel-acoustic/python/data .`
Per tornare al demone: `rm raccolta.json` e Run.

**Rete** (`nn.py`): in Edge Impulse addestrare sulla sola colonna `piezo` (2000 Hz, finestra
250 ms, ADC 12 bit), esportare "Linux (AARCH64)" e copiare il file come `python/modello.eim`
(`chmod +x`). Con il file presente il demone usa la rete; senza, le regole. A mano:

```bash
python3 nn.py modello.eim                                   # classi, assi, finestra del modello
python3 daemon.py --bridge --classifier nn --model modello.eim
python3 evaluate.py --data data/testing --classifier nn --model modello.eim   # rete
python3 evaluate.py --data data/testing                                        # regole, per il confronto
```

Sotto `--min-conf` (0,6) la rete risponde "incerto", che non fa mai scattare l'allarme.
