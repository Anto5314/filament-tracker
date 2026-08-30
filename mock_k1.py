#!/usr/bin/env python3
"""
MOCK imprimante Creality K1 — simule le WebSocket 9999 de la K1SE pour tester
le collecteur SANS avoir l'imprimante allumée.

Usage : python3 mock_k1.py [port] [file_name]
Simule : impression de demo_vase.gcode pendant ~40 s avec usedMaterialLength
qui remonte de 0 → 1842.6 mm, puis completed.

Comportement conforme au protocole Creality :
- reçoit {"method":"get","params":{"ReqPrinterPara":1}} → envoie l'état
- heartbeat ModeCode → répond "ok"
"""
import asyncio
import json
import sys
import time

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 9999
FILE_NAME = sys.argv[2] if len(sys.argv) > 2 else "demo_vase.gcode"
DURATION = 40          # secondes d'impression simulée
TOTAL_MM = 1842.6      # longueur filament totale


def status_payload(status, progress, mat_mm, job_time=0, left_time=300, err=None):
    p = {
        "method": "report",
        "printStatus": status,
        "dProgress": progress,
        "reqPrinterPara": {
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
    print(f"[mock] client connecté — impression simulée: {FILE_NAME} ({DURATION}s)")
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

        # requête d'état → on envoie l'état courant (simulé)
        params = msg.get("params") or {}
        if (msg.get("method") == "get" and
                (params.get("ReqPrinterPara") or params.get("reqPrintObjects"))):
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
    print(f"[mock] serveur K1 simulé sur ws://0.0.0.0:{PORT}")
    async with websockets.serve(handle, "0.0.0.0", PORT, ping_interval=None):
        await asyncio.Future()  # forever


if __name__ == "__main__":
    asyncio.run(main())