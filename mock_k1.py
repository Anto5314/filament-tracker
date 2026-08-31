#!/usr/bin/env python3
"""
MOCK imprimante Creality — simule le WebSocket 9999 de CrealityOS pour tester
le collecteur SANS avoir l'imprimante allumée.

Usage : python3 mock_k1.py [port] [file_name] [modele] [duree_secondes]
Exemples :
  python3 mock_k1.py 9999 demo_vase.gcode "K1 SE" 40
  python3 mock_k1.py 9999 vase.gcode "K2 Plus" 120
  python3 mock_k1.py 9999 test.gcode "Ender-3 V3 KE" 25

Simule : impression pendant ~DURATION s avec usedMaterialLength qui remonte
de 0 → TOTAL_MM, puis completed. Répond aussi à la combinaison complète de
requêtes (reqGcodeFile+reqGcodeList+reqHistory+...) pour tester la
bibliothèque miniatures + historique.

Comportement conforme au protocole Creality :
- reçoit {"method":"get","params":{...}} → renvoie état / fichiers / historique
- heartbeat ModeCode → répond "ok"
"""
import asyncio
import json
import sys
import time

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 9999
FILE_NAME = sys.argv[2] if len(sys.argv) > 2 else "demo_vase.gcode"
MODEL = sys.argv[3] if len(sys.argv) > 3 else "K1 SE"
DURATION = int(sys.argv[4]) if len(sys.argv) > 4 else 40
TOTAL_MM = 1842.6      # longueur filament totale

# Bibliothèque simulée (renvoyée à reqGcodeList / reqGcodeFileInfo2)
FILES = [
    {"name": FILE_NAME, "file_size": 421337, "material": "PLA",
     "thumbnail": "mock_demo", "filename": FILE_NAME,
     "filamentWeight": "0.00, 12.34", "timeCost": 2381,
     "consumables": 1842},
    {"name": "autre_tournevis.stl_PLA_45m.gcode", "file_size": 180255,
     "material": "PLA;PLA", "thumbnail": "mock_autre",
     "filename": "autre_tournevis.stl_PLA_45m.gcode",
     "filamentWeight": "0.00, 9.8", "timeCost": 2700,
     "consumables": 1500},
]

# Historique simulé (renvoyé à reqHistory)
def history_list(job_id=1001):
    return [{
        "id": job_id,
        "filename": FILE_NAME,
        "starttime": int(time.time()) - DURATION,
        "usagetime": DURATION,
        "usagematerial": TOTAL_MM,
        "printfinish": 1,
        "thumbnail": "mock_history",
    }]


def status_payload(status, progress, mat_mm, job_time=0, left_time=300, err=None):
    p = {
        "method": "report",
        "printStatus": status,
        "dProgress": progress,
        "reqPrinterPara": {
            "model": MODEL,
            "printFileName": FILE_NAME,
            "usedMaterialLength": mat_mm,
            "printJobTime": job_time,
            "printLeftTime": left_time,
            "realTimeFlow": 12.4 if status == "printing" else 0,
            "nozzleTemp": 210.0 if status == "printing" else 25.0,
            "bedTemp": 60.0 if status == "printing" else 22.0,
            "boxTemp": 32.0,
            "totalLayer": 128,
            "workingLayer": max(0, int(128 * progress)),
            "state": status,
            "err": err or {"errcode": 0},
        },
    }
    return p


async def handle(ws):
    print(f"[mock] client connecté — impression simulée: {FILE_NAME} ({DURATION}s) "
          f"modèle={MODEL}")
    start = time.time()
    while True:
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=DURATION + 30)
        except asyncio.TimeoutError:
            print("[mock] timeout recv — fin")
            break
        except Exception:
            break

        text = raw.decode() if isinstance(raw, (bytes, bytearray)) else raw
        if text == "ok":
            continue
        try:
            msg = json.loads(text)
        except Exception:
            await ws.send("ok")
            continue

        # heartbeat
        if msg.get("ModeCode") == "heart_beat":
            await ws.send("ok")
            continue

        params = msg.get("params") or {}
        if msg.get("method") != "get":
            # commande set / autre → ok
            await ws.send("ok")
            continue

        # --- Combinaison complète : fichiers + historique + état ---
        if (params.get("reqGcodeFile") or params.get("reqGcodeList")
                or params.get("reqPrintObjects")
                or params.get("reqHistory")):
            resp = {
                "method": "report",
                "retGcodeFileInfo2": FILES,
                "historyList": history_list(),
            }
            await ws.send(json.dumps(resp))
            await ws.send("ok")
            continue

        # requête d'état simple → on envoie l'état courant (simulé)
        if params.get("ReqPrinterPara") or params.get("reqPrintObjects"):
            pass  # géré par la combinaison complète ci-dessus ; fallback ci-dessous
        elapsed = time.time() - start
        if elapsed > DURATION:
            # impression terminée
            await ws.send(json.dumps(status_payload("completed", 1.0, TOTAL_MM,
                                                    job_time=int(DURATION), left_time=0)))
            print("[mock] impression SIMULÉE TERMINÉE ✅")
        elif elapsed < 3:
            await ws.send(json.dumps(status_payload("printing", 0.0, 0.0, 0, int(DURATION))))
        else:
            prog = min(0.99, elapsed / DURATION)
            mat = round(TOTAL_MM * prog, 1)
            await ws.send(json.dumps(status_payload("printing", round(prog, 3), mat,
                                                    int(elapsed), int(DURATION - elapsed)
                                                    + int(TOTAL_MM * (1 - prog) / 6))))
        await ws.send("ok")


async def main():
    import websockets
    print(f"[mock] serveur {'CrealityOS ' + MODEL} simulé sur ws://0.0.0.0:{PORT}")
    async with websockets.serve(handle, "0.0.0.0", PORT, ping_interval=None):
        await asyncio.Future()  # forever


if __name__ == "__main__":
    asyncio.run(main())