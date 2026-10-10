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
python3 fakemcu.py --n 500 --order random --interval 0.5 | python3 daemon.py --input - --siren silent
python3 dashboard.py          # http://<ip-uno-q>:8080
# con la scheda
python3 daemon.py --port /dev/ttyMCU                # sirena attiva di default con --port
python3 evaluate.py --data data/testing --triggers-per-hour 40
```
