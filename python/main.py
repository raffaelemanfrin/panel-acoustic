"""Avvio da App Lab (Run): demone + dashboard sulla UNO Q.

- il demone riceve i blocchi dalla MCU via Bridge (notifiche "pa_lines"),
  classifica, applica la logica, scrive in SQLite e suona la sirena;
- la dashboard e' su http://<ip-della-scheda>:8080 (porta esposta in app.yaml).

Per provare a mano senza App Lab (in SSH sulla scheda):
    python3 daemon.py --bridge
    python3 dashboard.py
"""
import logging
import threading
import time

from arduino.app_utils import App

import daemon
import dashboard

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")

threading.Thread(target=dashboard.main, args=(["--port", "8080"],), name="dashboard", daemon=True).start()
threading.Thread(target=daemon.main, args=(["--bridge", "--siren", "on"],), name="daemon", daemon=True).start()


def loop():
    time.sleep(10)


App.run(user_loop=loop)
