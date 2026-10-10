"""Avvio da App Lab (Run) sulla UNO Q.

Due modalita', scelte all'avvio:
  - normale: demone (classifica, logica, SQLite, sirena) + dashboard su
    http://<ip-della-scheda>:8080
  - raccolta: se esiste python/raccolta.json, registra i blocchi in CSV per
    Edge Impulse invece di classificarli (vedi raccolta.py). La dashboard
    resta accesa ma non riceve eventi.

Rete neurale: se esiste python/modello.eim il demone usa la rete al posto
delle regole (le regole restano il piano B: basta togliere il file).

Per provare a mano senza App Lab (in SSH sulla scheda):
    python3 daemon.py --bridge
    python3 dashboard.py
"""
import logging
import threading
import time
from pathlib import Path

from arduino.app_utils import App

import daemon
import dashboard
import raccolta

HERE = Path(__file__).resolve().parent
MODEL = HERE / "modello.eim"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("main")

threading.Thread(target=dashboard.main, args=(["--port", "8080"],), name="dashboard", daemon=True).start()

if raccolta.CONFIG.exists():
    log.info("raccolta.json presente: modalita' RACCOLTA (il demone non parte)")
    target, args = raccolta.run_on_bridge, ()
else:
    argv = ["--bridge", "--siren", "on"]
    if MODEL.exists():
        argv += ["--classifier", "nn", "--model", str(MODEL)]
        log.info("modello.eim presente: classificatore = rete")
    target, args = daemon.main, (argv,)
threading.Thread(target=target, args=args, name="worker", daemon=True).start()


def loop():
    time.sleep(10)


App.run(user_loop=loop)
