#!/usr/bin/env python3
"""
Collecteur K1SE → Spoolman + interface web
1) Écoute l'imprimante Creality K1SE (WebSocket 9999)
2) Journalise chaque session d'impression (fichier, statut, erreurs)
3) Sert l'interface web PWA (mobile) + API REST
4) Proxifie Spoolman et décompte la consommation réelle
"""
import asyncio
import json
import logging
import os
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import aiohttp
import aiohttp.web
from aiohttp import web
import websockets

log = logging.getLogger("k1")

K1_HOST = os.environ.get("K1_HOST", "192.168.1.41")
K1_PORT = int(os.environ.get("K1_PORT", "9999"))
K1_URL = f"ws://{K1_HOST}:{K1_PORT}"
WS_SUBPROTOCOL = os.environ.get("K1_SUBPROTOCOL", "")
DB_PATH = os.environ.get("DB_PATH", "/data/k1_sessions.db")
SPOOLMAN_URL = os.environ.get("SPOOLMAN_URL", "http://spoolman:8000")
WEB_PORT = int(os.environ.get("WEB_PORT", "8123"))
STATIC_DIR = os.environ.get("STATIC_DIR", "/app/static")
POLL_INTERVAL = float(os.environ.get("POLL_INTERVAL", "5"))
# Watchdog : si la K1 ne répond plus pendant ce délai → reconnexion forcée
K1_SILENCE_PROBE_SECS = float(os.environ.get("K1_SILENCE_PROBE_SECS", "8"))
K1_SILENCE_DEAD_SECS = float(os.environ.get("K1_SILENCE_DEAD_SECS", "30"))

Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)


# ---------- DB ----------
def init_db():
    con = sqlite3.connect(DB_PATH)
    con.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY,
            file_name TEXT,
            status TEXT,
            start_time TEXT,
            end_time TEXT,
            progress REAL DEFAULT 0,
            material_mm REAL,
            spool_id INTEGER,
            filament_grams REAL,
            thumb TEXT,
            error TEXT,
            metadata TEXT
        )
    """)
    con.commit()
    # Migration : ajoute les colonnes manquantes si la table existait déjà
    cols = {r[1] for r in con.execute("PRAGMA table_info(sessions)").fetchall()}
    for cdef in ("material_mm REAL", "thumb TEXT"):
        col = cdef.split()[0]
        if col not in cols:
            con.execute(f"ALTER TABLE sessions ADD COLUMN {cdef}")
            con.commit()
            log.warning("Migration : colonne ajoutée 'sessions.%s'", col)
    con.execute("""
        CREATE TABLE IF NOT EXISTS k1_files (
            name TEXT PRIMARY KEY,
            thumb TEXT,
            size INTEGER,
            material TEXT,
            grams REAL,
            consumables_mm REAL,
            time_s INTEGER,
            updated TEXT
        )
    """)
    # Migration k1_files : colonnes quantité
    try:
        cols = {r[1] for r in con.execute("PRAGMA table_info(k1_files)").fetchall()}
        for cdef in ("grams REAL", "consumables_mm REAL", "time_s INTEGER"):
            col = cdef.split()[0]
            if col not in cols:
                con.execute(f"ALTER TABLE k1_files ADD COLUMN {cdef}")
                con.commit()
    except Exception:
        pass
    con.commit()
    con.close()


def db_conn():
    return sqlite3.connect(DB_PATH)


def add_session(sid, file_name, status, start_time):
    con = db_conn()
    con.execute("INSERT OR IGNORE INTO sessions (id, file_name, status, start_time) VALUES (?,?,?,?)",
                (sid, file_name, status, start_time))
    con.commit()
    con.close()


def update_session(sid, **fields):
    allowed = {"status", "end_time", "progress", "material_mm", "spool_id",
               "filament_grams", "thumb", "error"}
    keys = [k for k in fields if k in allowed and fields[k] is not None]
    if not keys:
        return
    sets = ", ".join(f"{k} = ?" for k in keys)
    vals = [fields[k] for k in keys] + [sid]
    con = db_conn()
    con.execute(f"UPDATE sessions SET {sets} WHERE id = ?", vals)
    con.commit()
    con.close()


def get_session(sid):
    con = db_conn()
    row = con.execute("SELECT * FROM sessions WHERE id = ?", (sid,)).fetchone()
    if not row:
        con.close()
        return None
    cols = [d[0] for d in con.execute("SELECT * FROM sessions LIMIT 1").description]
    con.close()
    return dict(zip(cols, row))


def list_sessions(limit=300):
    con = db_conn()
    rows = con.execute("SELECT * FROM sessions ORDER BY start_time DESC LIMIT ?", (limit,)).fetchall()
    cols = [d[0] for d in con.execute("SELECT * FROM sessions LIMIT 1").description]
    con.close()
    return [dict(zip(cols, r)) for r in rows]


def monthly_stats():
    """Stats agrégées par mois (année-mois) : impressions, erreurs, filament (g),
    durée totale (h), consommation moyenne par impression, dernière session."""
    from collections import OrderedDict
    con = db_conn()
    cols = [d[0] for d in con.execute("SELECT * FROM sessions LIMIT 1").description]
    rows = con.execute("SELECT start_time, end_time, status, filament_grams, material_mm "
                       "FROM sessions").fetchall()
    con.close()
    months = OrderedDict()
    for start_time, end_time, status, grams, mm in rows:
        if not start_time:
            continue
        try:
            d = datetime.fromisoformat(start_time)
        except Exception:
            continue
        key = d.strftime("%Y-%m")
        # Nom du mois en français
        mois_fr = ["janvier", "février", "mars", "avril", "mai", "juin",
                   "juillet", "août", "septembre", "octobre", "novembre", "décembre"]
        label = f"{mois_fr[d.month - 1].capitalize()} {d.year}"
        m = months.setdefault(key, {
            "month": key, "label": label, "prints": 0,
            "completed": 0, "errors": 0, "stopped": 0, "grams": 0.0,
            "mm": 0.0, "seconds": 0, "avg_grams": 0.0,
        })
        m["prints"] += 1
        if status == "completed":
            m["completed"] += 1
        elif status == "error":
            m["errors"] += 1
        elif status == "stopped":
            m["stopped"] += 1
        try:
            m["grams"] += float(grams or 0)
        except (TypeError, ValueError):
            pass
        try:
            m["mm"] += float(mm or 0)
        except (TypeError, ValueError):
            pass
        if end_time:
            try:
                t0 = datetime.fromisoformat(start_time)
                t1 = datetime.fromisoformat(end_time)
                m["seconds"] += max(0, (t1 - t0).total_seconds())
            except Exception:
                pass
    # ordre décroissant (mois récents en premier) + moyenne par impression
    out = list(months.values())[::-1]
    for m in out:
        m["avg_grams"] = round(m["grams"] / m["prints"], 2) if m["prints"] else 0
        m["grams"] = round(m["grams"], 2)
        m["mm"] = round(m["mm"], 1)
        m["seconds"] = int(m["seconds"])
        m["hours"] = round(m["seconds"] / 3600, 1)
    # Totaux globaux
    total = {"prints": sum(m["prints"] for m in out),
             "completed": sum(m["completed"] for m in out),
             "errors": sum(m["errors"] for m in out),
             "stopped": sum(m["stopped"] for m in out),
             "grams": round(sum(m["grams"] for m in out), 2),
             "mm": round(sum(m["mm"] for m in out), 1),
             "hours": round(sum(m["hours"] for m in out), 1)}
    return {"months": out, "total": total}


async def api_stats(request):
    """GET /api/stats — stats mensuelles + totaux."""
    return aiohttp.web.json_response(monthly_stats())


# ---------- Client K1 ----------
# Carte des états numériques du firmware Creality K1 (const PRINTER_STATE_MAP)
K1_STATE_MAP = {"0": "stopped", "1": "printing", "2": "completed",
                "3": "error", "4": "stopped", "5": "paused"}


def _norm_status(raw):
    if not raw and raw != 0:
        return None
    # état numérique du firmware K1
    if isinstance(raw, (int, float)) or (isinstance(raw, str) and raw.strip().isdigit()):
        return K1_STATE_MAP.get(str(raw).strip())
    s = str(raw).lower()
    if isinstance(raw, dict):
        s = str(raw.get("status", "")).lower()
    if s in ("second", "sla", "printing", "print", "printing_start", "printingstart",
             "printinging", "1"):
        return "printing"
    if s in ("finish", "completed", "complete", "success", "print_success", "2"):
        return "completed"
    if s in ("stop", "stopped", "abort", "aborted", "stopped_by_user", "0", "4"):
        return "stopped"
    if s in ("fail", "failed", "error", "exception", "print_failed", "3"):
        return "error"
    if s in ("pause", "paused", "5"):
        return "paused"
    return s or None


def _extract_model(payload):
    """Nom du modèle : cherché à la racine ET dans reqPrinterPara (les firmwares
    CrealityOS varient selon la famille)."""
    inner = payload.get("reqPrinterPara") or payload.get("printPara") or {}
    candidates = [payload, inner] if isinstance(inner, dict) else [payload]
    for src in candidates:
        for key in ("model", "modelName", "printerModel", "deviceModel"):
            v = src.get(key)
            if v:
                return str(v)
    return None


def _extract_file(payload):
    # Certains firmwares imbriquent les infos dans reqPrinterPara
    inner = payload.get("reqPrinterPara") or payload.get("printPara") or {}
    candidates = [payload, inner] if isinstance(inner, dict) else [payload]
    for src in candidates:
        for key in ("fileName", "printFileName", "printJobName", "modelName",
                    "PrintFileName", "print_file_name", "gcodeName"):
            v = src.get(key)
            if v:
                return str(v)
    return None


def _extract_material_mm(payload):
    """Longueur de filament utilisée envoyée par l'imprimante (mm ou cm selon version).
    Clés observées dans les intégrations Creality : usedMaterialLength (mm), materialLength (cm)."""
    inner = payload.get("reqPrinterPara") or payload.get("printPara") or {}
    candidates = [payload, inner] if isinstance(inner, dict) else [payload]
    for src in candidates:
        for key in ("usedMaterialLength", "materialLength", "used_material_length"):
            v = src.get(key)
            try:
                return float(v)
            except (TypeError, ValueError):
                continue
    return None


class K1Client:
    def __init__(self, url, on_state):
        self.url = url
        self.on_state = on_state
        self.ws = None
        self._last_rx = 0.0
        self._connected_at = 0.0

    async def run(self):
        while True:
            try:
                kwargs = {"ping_interval": None}
                if WS_SUBPROTOCOL:
                    kwargs["subprotocols"] = [WS_SUBPROTOCOL]
                async with websockets.connect(self.url, **kwargs) as ws:
                    self.ws = ws
                    self._last_rx = 0.0
                    self._connected_at = time.monotonic()
                    log.info("Connecté K1 %s", self.url)
                    tracker.mark_connected(True)
                    await self._loop(ws)
            except Exception as exc:
                tracker.mark_connected(False)
                log.warning("Connexion K1 perdue (%s), reconnexion dans 10s", exc)
                await asyncio.sleep(10)

    async def _loop(self, ws):
        async def heartbeat():
            # Watchdog de silence : si l'imprimante ne répond plus (coupée,
            # veille, réseau), on ferme et on se reconnecte.
            while True:
                await asyncio.sleep(5)
                try:
                    await ws.send(json.dumps({"ModeCode": "heart_beat"}))
                except Exception:
                    return
                now = time.monotonic()
                last = self._last_rx or self._connected_at
                silence = now - last
                # probe pour tenter de réveiller
                if silence > K1_SILENCE_PROBE_SECS:
                    try:
                        await ws.send(json.dumps({"method": "get", "params": {"ReqPrinterPara": 1}}))
                    except Exception:
                        pass
                # connexion morte : forcer la reconnexion
                if silence > K1_SILENCE_DEAD_SECS:
                    log.warning("K1 muette depuis %.0fs — reconnexion forcée", silence)
                    tracker.mark_connected(False)
                    try:
                        await ws.close()
                    except Exception:
                        pass
                    return

        async def poller():
            await asyncio.sleep(2)
            ticks = 0
            while True:
                await asyncio.sleep(POLL_INTERVAL)
                try:
                    await ws.send(json.dumps({"method": "get", "params": {"ReqPrinterPara": 1}}))
                    await ws.send(json.dumps({"method": "get", "params": {"reqPrintObjects": 1}}))
                    # demande périodique (~1/min) de la liste des fichiers → miniatures
                    # (la K1 ne répond retGcodeFileInfo2 qu'avec la combinaison complète)
                    ticks += 1
                    if ticks % max(1, int(60 // max(POLL_INTERVAL, 0.1))) == 0:
                        await ws.send(json.dumps({"method": "get", "params": {
                            "reqGcodeFile": 1, "reqGcodeList": 1, "reqHistory": 1,
                            "reqElapseVideoList": 1, "reqPrintObjects": 1,
                            "reqMaterialBoxsInfo": 1}}))
                except Exception:
                    return

        hb = asyncio.create_task(heartbeat())
        pl = asyncio.create_task(poller())
        try:
            async for raw in ws:
                self._last_rx = time.monotonic()
                text = raw.decode("utf-8", "ignore") if isinstance(raw, (bytes, bytearray)) else raw
                if text == "ok":
                    continue
                try:
                    payload = json.loads(text)
                except Exception:
                    continue
                if not isinstance(payload, dict):
                    continue
                if payload.get("ModeCode") == "heart_beat":
                    continue
                try:
                    await ws.send("ok")
                except Exception:
                    pass
                # réponse à reqGcodeList → la liste des fichiers de l'imprimante
                flist = payload.get("retGcodeFileInfo2") or payload.get("retGcodeFileInfo")
                if flist is not None and isinstance(flist, list):
                    asyncio.create_task(
                        _sync_k1_thumbnails(flist))
                # réponse à reqHistory → l'historique des impressions (rétroactif)
                hlist = payload.get("historyList")
                if hlist is not None and isinstance(hlist, list):
                    asyncio.create_task(
                        _import_k1_history(hlist))
                await self.on_state(payload)
        finally:
            hb.cancel()
            pl.cancel()


# ---------- Tracker de sessions ----------
class Tracker:
    def __init__(self):
        self.current = None
        self.last_status = None
        self.last_file = None
        self.last_progress = 0.0
        self.ws_connected = False
        self.last_payload = None

    def mark_connected(self, connected: bool):
        self.ws_connected = connected

    async def handle(self, payload):
        # Fusion progressive : la K1 envoie des pushs incrémentaux (nozzleTemp seul,
        # curPosition…) et des payloads complets périodiques. On garde les champs
        # précédents pour avoir une vue stable et complète.
        if self.last_payload:
            merged = dict(self.last_payload)
            for k, v in payload.items():
                if v is not None and v != "":
                    merged[k] = v
            self.last_payload = merged
        else:
            self.last_payload = payload
        payload = self.last_payload
        inner = payload.get("reqPrinterPara") or payload.get("printPara") or {}
        inner = inner if isinstance(inner, dict) else {}
        status = _norm_status(payload.get("status") or payload.get("PrintStatus")
                              or payload.get("state") or payload.get("printStatus")
                              or inner.get("state") or inner.get("printStatus"))
        file_name = _extract_file(payload)
        prog = (payload.get("progress", payload.get("PrintProgress",
                payload.get("dProgress", self.last_progress))))
        # dProgress imbriqué
        if prog is None and inner.get("dProgress") is not None:
            prog = inner["dProgress"]
        try:
            prog = float(prog)
        except Exception:
            prog = self.last_progress

        now = datetime.now(timezone.utc).isoformat()
        mat_mm = _extract_material_mm(payload)

        if status == "printing":
            if self.current is None:
                sid = uuid.uuid4().hex[:12]
                add_session(sid, file_name or self.last_file or "inconnu", "printing", now)
                self.current = sid
                log.info("▶ Début impression: %s (%s)", file_name, sid)
                # Course live/historique : l'import k1h peut avoir été créé AVANT
                # la session live (historique périodique qui voit l'impression en
                # cours). On supprime les k1h jumeaux (même fichier, ±3 min) —
                # la session live fait foi pour la suite.
                _purge_duplicate_k1h(file_name or self.last_file or "inconnu", now)
            else:
                update_session(self.current, progress=prog, material_mm=mat_mm)
        elif status in ("completed", "error", "stopped", "paused"):
            if self.current:
                err = None
                if status == "error":
                    err = str(payload.get("error") or payload.get("errno") or "erreur d'impression")
                update_session(self.current, status=status, end_time=now,
                               progress=prog, material_mm=mat_mm, error=err)
                log.info("■ Fin (%s) %s — %s at %.0f%%%s%s", status, self.current,
                         file_name or self.last_file, prog * 100,
                         f" err={err}" if err else "",
                         f" mat={mat_mm:.0f} mm" if mat_mm else "")
                self.last_status = status
                self.current = None
                return
        if status:
            self.last_status = status
        self.last_progress = prog
        if file_name:
            self.last_file = file_name


# ---------- Spoolman ----------
async def spoolman_get(client, path):
    async with client.get(f"{SPOOLMAN_URL}/api/v1{path}") as r:
        text = await r.text()
        try:
            return json.loads(text), r.status
        except Exception:
            return {"raw": text}, r.status


async def spoolman_patch(client, path, data):
    async with client.patch(f"{SPOOLMAN_URL}/api/v1{path}", json=data) as r:
        text = await r.text()
        try:
            return json.loads(text), r.status
        except Exception:
            return {"raw": text}, r.status


# ---------- App HTTP ----------
async def api_sessions(request):
    data = list_sessions()
    # enrichir avec URL miniature
    for s in data:
        if s.get("thumb"):
            s["thumb_url"] = f"/thumbs/{s['thumb']}"
    return aiohttp.web.json_response(data)


async def api_sessions_attach(request):
    """POST /api/sessions/{id}/spool  body: {"spool_id": 3, "filament_grams": 12.4, "debit": true}
    Si filament_grams absent mais material_mm présent (mesure imprimante), on convertit
    mm → g grâce à la densité du filament (Spoolman).
    debit=false → associe SANS décompter le poids de la bobine (pour les anciennes
    sessions déjà consommées avant la pesée des bobines)."""
    sid = request.match_info["id"]
    try:
        body = await request.json()
    except Exception:
        body = {}
    spool_id = body.get("spool_id")
    grams = body.get("filament_grams")
    debit = body.get("debit", True)  # défaut: décompte (sessions live)
    sess = get_session(sid)
    if not sess:
        return aiohttp.web.json_response({"error": "session introuvable"}, status=404)

    # Conversion automatique material_mm → grammes via densité du filament
    if grams is None and sess.get("material_mm") and spool_id:
        try:
            async with aiohttp.ClientSession() as client:
                res, status = await spoolman_get(client, f"/spool/{spool_id}")
                density = None
                if status == 200 and isinstance(res, dict):
                    fil = res.get("filament") or {}
                    density = fil.get("density")
                if density:
                    from gcode_parser import mm_to_grams
                    grams = mm_to_grams(float(sess["material_mm"]), density=density or 1.24)
                    log.info("Conversion auto: %s mm × densité %s = %s g", sess["material_mm"], density, grams)
        except Exception as exc:
            # Spoolman down : on associe quand même la bobine, sans conversion
            log.warning("Spoolman injoignable (conversion densité): %s", exc)

    update_session(sid, spool_id=spool_id, filament_grams=grams)
    # décompte Spoolman (désactivé via debit=false)
    if spool_id and grams and debit:
        try:
            async with aiohttp.ClientSession() as client:
                res, status = await spoolman_get(client, f"/spool/{spool_id}")
                if status == 200 and isinstance(res, dict):
                    cur = res.get("remaining_weight") or 0
                    new_rem = max(0, round(cur - grams, 2))
                    await spoolman_patch(client, f"/spool/{spool_id}", {"remaining_weight": new_rem})
        except Exception as exc:
            # Ne pas faire échouer l'association si Spoolman est momentanément down
            log.warning("Spoolman injoignable (décompte): %s", exc)
    return aiohttp.web.json_response(get_session(sid))


async def api_spools(request):
    """Proxy Spoolman : liste des spools (filament + poids)."""
    try:
        async with aiohttp.ClientSession() as client:
            res, status = await spoolman_get(client, "/spool")
    except Exception as exc:
        log.warning("Spoolman injoignable: %s", exc)
        return aiohttp.web.json_response({"error": "spoolman injoignable"}, status=502)
    if status != 200:
        return aiohttp.web.json_response({"error": "spoolman injoignable"}, status=502)
    return aiohttp.web.json_response(res)


THUMB_DIR = os.environ.get("THUMB_DIR", "/data/thumbs")


async def _save_thumb(data: bytes, mime: str) -> str:
    """Sauvegarde une miniature et renvoie son id (pour /thumbs/{id})."""
    import hashlib
    tid = hashlib.sha1(data).hexdigest()[:16]
    ext = "png" if "png" in mime else "jpg"
    Path(THUMB_DIR).mkdir(parents=True, exist_ok=True)
    path = Path(THUMB_DIR) / f"{tid}.{ext}"
    if not path.exists():
        path.write_bytes(data)
    return tid


def get_thumb_id_for_filename(fname: str) -> str | None:
    """Retourne le thumb_id déjà mémorisé pour un nom de fichier (bibliothèque K1)."""
    try:
        con = db_conn()
        row = con.execute(
            "SELECT thumb FROM k1_files WHERE name = ?", (fname,)).fetchone()
        con.close()
        return row[0] if row else None
    except Exception:
        return None


def _k1_files_upsert(entries: list[dict]):
    try:
        con = db_conn()
        now = datetime.now(timezone.utc).isoformat()
        for e in entries:
            con.execute(
                "INSERT OR REPLACE INTO k1_files (name, thumb, size, material, grams, "
                "consumables_mm, time_s, updated) VALUES (?,?,?,?,?,?,?,?)",
                (e["name"], e.get("thumb"), e.get("size"), e.get("material"),
                 e.get("grams"), e.get("consumables_mm"), e.get("time_s"), now))
        con.commit()
        con.close()
    except Exception:
        pass


async def _sync_k1_thumbnails(flist: list):
    """Télécharge les miniatures des fichiers de l'imprimante (port 80),
    les stocke dans /data/thumbs et mémorise le mapping nom → thumb."""
    try:
        # noms déjà mémorisés
        known = {}
        try:
            con = db_conn()
            for r in con.execute("SELECT name, thumb FROM k1_files").fetchall():
                known[r[0]] = r[1]
            con.close()
        except Exception:
            pass
        entries = []
        need = []
        for item in flist:
            if not isinstance(item, dict):
                continue
            name = item.get("name") or item.get("gcode") or ""
            if not name or not name.lower().endswith(".gcode"):
                continue
            # filamentWeight : "0.00, 25.07" → somme des extrudeurs = grammes
            grams = None
            fw = item.get("filamentWeight")
            if fw:
                try:
                    vals = [float(v.strip()) for v in str(fw).replace(";", ",").split(",") if v.strip()]
                    if vals:
                        grams = round(sum(vals), 2)
                except Exception:
                    grams = None
            entries.append({
                "name": name,
                "thumb": known.get(name),
                "size": item.get("file_size") or item.get("size"),
                "material": item.get("material"),
                "grams": grams,
                "consumables_mm": item.get("consumables") or item.get("filamentLength"),
                "time_s": item.get("timeCost") or item.get("timecost"),
            })
            if name not in known or not known.get(name):
                need.append(name)
        # télécharger les miniatures manquantes
        if need:
            async with aiohttp.ClientSession() as s:
                for name in need:
                    try:
                        base = name[:-6] if name.lower().endswith(".gcode") else name
                        import urllib.parse
                        url = f"http://{K1_HOST}/downloads/humbnail/{urllib.parse.quote(base)}.png"
                        async with s.get(url, timeout=5) as r:
                            if r.status == 200:
                                data = await r.read()
                                if len(data) > 50:
                                    tid = await _save_thumb(data, "image/png")
                                    for e in entries:
                                        if e["name"] == name:
                                            e["thumb"] = tid
                    except Exception:
                        continue
        _k1_files_upsert(entries)
        log.info("Sync miniatures K1 : %d fichiers (dont %d téléchargés)", len(entries), len(need))
    except Exception as exc:
        log.warning("Sync miniatures K1 échoué: %s", exc)


def _purge_duplicate_k1h(file_name: str, start_time: str):
    """Supprime les sessions historiques k1h* qui sont des jumeaux d'une
    impression suivie en LIVE (même fichier, début ±3 min). La session live
    fait foi : le k1h est un artefact créé par l'import historique périodique
    qui a vu l'impression en cours AVANT la création de la session live."""
    if not file_name or not start_time:
        return
    try:
        from datetime import timedelta
        t0 = datetime.fromisoformat(start_time)
        lo = (t0 - timedelta(minutes=3)).isoformat()
        hi = (t0 + timedelta(minutes=3)).isoformat()
        base = os.path.basename(file_name)
        con = db_conn()
        rows = con.execute(
            "SELECT id FROM sessions WHERE id LIKE 'k1h%' "
            "AND (file_name = ? OR file_name LIKE ?) "
            "AND start_time BETWEEN ? AND ?",
            (base, f"%{base}%", lo, hi)).fetchall()
        for (kid,) in rows:
            con.execute("DELETE FROM sessions WHERE id = ?", (kid,))
            log.info("Purge doublon historique: %s (jumeau live %s)", kid, file_name)
        con.commit()
        con.close()
    except Exception as exc:
        log.warning("Purge k1h jumeaux échouée: %s", exc)


def _k1_history_thumb(h: dict) -> str | None:
    """Miniature d'une entrée d'historique K1 : utilise le thumb du fichier
    correspondant dans la bibliothèque (les thumbs d'historique id-based ne
    sont pas servis en HTTP sur ce firmware)."""
    fn = h.get("filename") or ""
    base = os.path.splitext(os.path.basename(fn))[0]
    if not base:
        return None
    try:
        con = db_conn()
        row = con.execute(
            "SELECT thumb FROM k1_files WHERE name LIKE ? LIMIT 1",
            (f"%{base}%",)).fetchone()
        con.close()
        return row[0] if row and row[0] else None
    except Exception:
        return None


async def _import_k1_history(hlist: list):
    """Importe l'historique d'impressions de l'imprimante dans le journal
    (rétroactif). Chaque job K1 a un id unique → même fichier imprimé plusieurs
    fois = plusieurs sessions. printfinish: 1=terminé, 0/autre=arrêté/échec.
    Idempotent : INSERT OR IGNORE par session_id dérivé du job K1."""
    try:
        from gcode_parser import mm_to_grams
        imported = 0
        for h in hlist:
            if not isinstance(h, dict):
                continue
            job_id = h.get("id")
            fn = h.get("filename") or ""
            if job_id is None or not fn:
                continue
            name = os.path.basename(fn)
            # id stable dérivé du job K1 (l'id historique n'est pas fiable comme clé
            # si réinitialisé après formatage : on hache le couple fichier+début)
            import hashlib
            shash = hashlib.sha1(f"{job_id}|{fn}".encode()).hexdigest()[:16]
            sid = "k1h" + shash
            # statut : printfinish 1 → completed ; sinon arrêté/échec
            pf = h.get("printfinish")
            status = "completed" if pf in (1, True, "1", "true") else "stopped"
            if pf in (2, 3, "2", "3"):
                status = "error"
            # temps : starttime (epoch s) + durée usagetime (s)
            start_ts = h.get("starttime") or h.get("ctime")
            if start_ts:
                start_time = datetime.fromtimestamp(float(start_ts), timezone.utc).isoformat()
            else:
                start_time = None
            end_time = None
            ut = h.get("usagetime")
            if start_ts and ut:
                end_time = datetime.fromtimestamp(float(start_ts) + float(ut),
                                                  timezone.utc).isoformat()
            # filament mesuré par l'imprimante
            mat_mm = h.get("usagematerial")
            grams = None
            if mat_mm:
                try:
                    mat_mm = float(mat_mm)
                    grams = mm_to_grams(mat_mm, density=1.24)
                except Exception:
                    mat_mm = None
            thumb = _k1_history_thumb(h)
            # insérer (ignorer si déjà présent → dédoublonnage par id k1h)
            con = db_conn()
            cur = con.execute(
                "SELECT COUNT(*) FROM sessions WHERE id = ?", (sid,))
            exists = cur.fetchone()[0] > 0
            # Dédoublonnage LIVE : le tracker a peut-être déjà créé une session
            # en direct pendant l'impression (id ≠ k1h). On cherche une session
            # non-historique avec le même fichier débutée dans ±3 min.
            dup_live = None
            if start_time:
                try:
                    from datetime import timedelta
                    t0 = datetime.fromisoformat(start_time)
                    lo = (t0 - timedelta(minutes=3)).isoformat()
                    hi = (t0 + timedelta(minutes=3)).isoformat()
                    row = con.execute(
                        "SELECT id FROM sessions WHERE id NOT LIKE 'k1h%' "
                        "AND (file_name = ? OR file_name LIKE ?) "
                        "AND start_time BETWEEN ? AND ? LIMIT 1",
                        (name, f"%{os.path.basename(name)}%", lo, hi)).fetchone()
                    if row:
                        dup_live = row[0]
                except Exception:
                    dup_live = None
            if dup_live:
                # fusionner dans la session live (elle garde son id), sans créer
                # de doublon : on complète les données historiques manquantes
                live = get_session(dup_live)
                if live:
                    upd = {}
                    if not live.get("material_mm") and mat_mm:
                        upd["material_mm"] = mat_mm
                    if not live.get("filament_grams") and grams:
                        upd["filament_grams"] = grams
                    if not live.get("end_time") and end_time:
                        upd["end_time"] = end_time
                    # même fichier, même début → même impression. Prudence :
                    # l'historique peut porter un printfinish=0 artefactuel
                    # (entrée vue en cours → "stopped"; mm=0). On ne rétrograde
                    # JAMAIS une live "completed", et on ne remplace pas des
                    # mm réels par 0. La live (WS temps réel) fait foi.
                    if status == "completed" and live.get("status") != "completed":
                        upd["status"] = "completed"
                    elif status != "completed" and not live.get("end_time") \
                            and live.get("status") in ("printing", None):
                        upd["status"] = status
                    if thumb and not live.get("thumb"):
                        upd["thumb"] = thumb
                    if upd:
                        update_session(dup_live, **upd)
                # La session live fait foi : si un k1h dupliqué existait déjà
                # (créé avant la live lors de la course), on le supprime.
                if exists:
                    con.execute("DELETE FROM sessions WHERE id = ?", (sid,))
                    con.commit()
                    log.info("Doublon historique supprimé (déjà suivi en live): %s", sid)
            elif not exists:
                add_session(sid, name, status, start_time)
                updates = {"end_time": end_time, "material_mm": mat_mm,
                           "filament_grams": grams, "progress": 1.0}
                if thumb:
                    updates["thumb"] = thumb
                update_session(sid, **updates)
                imported += 1
            con.close()
        if imported:
            log.info("Historique K1 importé : %d nouvelles sessions (rétroactif)", imported)
    except Exception as exc:
        log.warning("Import historique K1 échoué: %s", exc)


async def api_thumb(request):
    """GET /thumbs/{tid} — sert la miniature d'une session."""
    tid = request.match_info["tid"]
    if "/" in tid or ".." in tid or not tid:
        return aiohttp.web.Response(status=404)
    ext = "png" if tid.endswith(".png") else "jpg"
    base = tid[:-4] if tid.endswith(".png") else tid[:-4] if tid.endswith(".jpg") else tid
    for cand in (f"{base}.png", f"{base}.jpg"):
        p = Path(THUMB_DIR) / cand
        if p.is_file():
            ctype = "image/png" if cand.endswith(".png") else "image/jpeg"
            return aiohttp.web.FileResponse(p, headers={"Content-Type": ctype,
                                                        "Cache-Control": "max-age=86400"})
    return aiohttp.web.Response(status=404)


async def api_manual_create(request):
    """PUT /api/sessions/{id}/manual  body: {"file_name": "...", "thumb_id": "..."} — création manuelle."""
    sid = request.match_info["id"]
    try:
        body = await request.json()
    except Exception:
        body = {}
    file_name = body.get("file_name") or "manuel"
    thumb_id = body.get("thumb_id")
    now = datetime.now(timezone.utc).isoformat()
    add_session(sid, file_name, "completed", now)
    update_session(sid, end_time=now, progress=1.0, thumb=thumb_id)
    return aiohttp.web.json_response(get_session(sid))


async def api_set_status(request):
    """POST /api/sessions/{id}/status  body: {"status": "error", "error": "..."}"""
    sid = request.match_info["id"]
    try:
        body = await request.json()
    except Exception:
        body = {}
    status = body.get("status")
    error = body.get("error")
    if status not in ("completed", "error", "stopped", "paused", "printing"):
        return aiohttp.web.json_response({"error": "statut invalide"}, status=400)
    update_session(sid, status=status, end_time=datetime.now(timezone.utc).isoformat(),
                   error=error)
    return aiohttp.web.json_response(get_session(sid))


async def api_sessions_link_gcode(request):
    """POST /api/sessions/{id}/gcode — lie une analyse .gcode à une session du journal.
    Body: {"file_name": "...", "thumb_id": "...", "grams_estimate": 6.4}
    Met à jour file_name (si vide), thumbnail, et la conso si la session n'a pas
    déjà une mesure réelle (material_mm de l'imprimante)."""
    sid = request.match_info["id"]
    try:
        body = await request.json()
    except Exception:
        body = {}
    sess = get_session(sid)
    if not sess:
        return aiohttp.web.json_response({"error": "session introuvable"}, status=404)
    fields = {}
    if body.get("file_name") and not sess.get("file_name") or body.get("file_name") in ("inconnu", "manuel"):
        fields["file_name"] = body["file_name"]
    if body.get("thumb_id"):
        fields["thumb"] = body["thumb_id"]
    # la mesure réelle (imprimante) prime ; sinon on prend l'estimation du slicer
    if body.get("grams_estimate") is not None and not sess.get("material_mm"):
        fields["filament_grams"] = float(body["grams_estimate"])
    if fields:
        update_session(sid, **fields)
    return aiohttp.web.json_response(get_session(sid))


async def api_analyze_gcode(request):
    """POST /api/gcode/analyze — upload d'un fichier .gcode (multipart) → parse filament + miniature.
    Renvoie {file_name, mm, grams_estimate, matched, thumb_id}"""
    from gcode_parser import parse_filament, mm_to_grams, extract_thumbnail
    reader = await request.multipart()
    field = await reader.next()
    if field is None or field.name != "file":
        return aiohttp.web.json_response({"error": "champ 'file' manquant (multipart)"}, status=400)
    filename = field.filename or "fichier.gcode"
    ext = os.path.splitext(filename)[1].lower()
    if ext not in (".gcode", ".g", ".gco", ".gc"):
        return aiohttp.web.json_response(
            {"error": "format non supporté (attendu .gcode/.g/.gco)"}, status=400)
    data = await field.read()
    if len(data) > 50 * 1024 * 1024:
        return aiohttp.web.json_response({"error": "fichier trop gros (>50 Mo)"}, status=400)
    try:
        text = data.decode("utf-8", errors="replace")
    except Exception:
        text = data.decode("latin-1", errors="replace")
    res = parse_filament(text)
    grams = None
    if res.get("g") is not None:
        grams = res["g"]
    elif res.get("mm") is not None:
        grams = mm_to_grams(res["mm"])
    # Miniature : seul format .gcode contient généralement des thumbnails
    thumb_id = None
    if ext in (".gcode", ".g", ".gc"):
        thumb = extract_thumbnail(text)
        if thumb:
            thumb_id = await _save_thumb(thumb["data"], thumb["mime"])
    if not thumb_id and filename:
        # Fallback : miniature déjà téléchargée depuis la bibliothèque K1
        base = os.path.splitext(os.path.basename(filename))[0]
        try:
            con = db_conn()
            row = con.execute(
                "SELECT thumb FROM k1_files WHERE name = ? OR name LIKE ?",
                (filename, f"%{base}%")).fetchone()
            con.close()
            if row and row[0]:
                thumb_id = row[0]
        except Exception:
            pass
    # Matching automatique : le .gcode uploadé (depuis ton PC) porte un nom
    # qui correspond souvent au fichier stocké sur l'imprimante (printFileName).
    # On cherche une session récente (24h) au nom identique (basename, sans
    # extension) pour la lier automatiquement → vignette + conso.
    linked_session_id = None
    try:
        base = os.path.splitext(os.path.basename(filename))[0].strip().lower()
        if base and not res.get("session_id"):
            con = db_conn()
            from datetime import timedelta
            cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
            rows = con.execute(
                "SELECT id, file_name, status, material_mm FROM sessions "
                "WHERE start_time >= ? AND status IN ('completed','error','stopped') "
                "ORDER BY start_time DESC", (cutoff,)).fetchall()
            con.close()
            for sid, sname, sstatus, smm in rows:
                if not sname:
                    continue
                sbase = os.path.splitext(os.path.basename(str(sname)))[0].strip().lower()
                if sbase and sbase == base:
                    link_fields = {}
                    if thumb_id and not get_session(sid).get("thumb"):
                        link_fields["thumb"] = thumb_id
                    if grams is not None and not smm:
                        link_fields["filament_grams"] = grams
                    if link_fields:
                        update_session(sid, **link_fields)
                    linked_session_id = sid
                    break
    except Exception as exc:
        log.warning("Matching auto session échoué: %s", exc)
    return aiohttp.web.json_response({
        "file_name": filename,
        "mm": res.get("mm"),
        "g": res.get("g"),
        "raw_mm": res.get("raw_mm"),
        "grams_estimate": grams,
        "thumb_id": thumb_id,
        "linked_session_id": linked_session_id,
        "matched": res.get("g") is not None or res.get("mm") is not None,
    })


async def api_health(request):
    lp = tracker.last_payload or {}
    return aiohttp.web.json_response({
        "ok": True,
        "k1_url": K1_URL,
        "spoolman": SPOOLMAN_URL,
        "ws_connected": tracker.ws_connected,
        "printing": tracker.current is not None,
        "k1_model": _extract_model(lp),
        "k1_state": lp.get("state"),
        "k1_nozzle": lp.get("nozzleTemp"),
        "k1_bed": lp.get("bedTemp0"),
        "k1_file": lp.get("printFileName") or lp.get("printJobName"),
        "k1_used_mm": lp.get("usedMaterialLength"),
    })


async def api_k1_files(request):
    """GET /api/k1/files — bibliothèque des fichiers de l'imprimante (avec miniatures).
    Données mémorisées par le sync WS (retGcodeFileInfo2, rafraîchi ~1/min en fond)."""
    try:
        con = db_conn()
        rows = con.execute(
            "SELECT name, thumb, size, material, grams, consumables_mm, time_s, updated "
            "FROM k1_files ORDER BY updated DESC").fetchall()
        con.close()
        data = [{"name": r[0], "thumb_url": f"/thumbs/{r[1]}" if r[1] else None,
                 "size": r[2], "material": r[3], "grams": r[4],
                 "consumables_mm": r[5], "time_s": r[6], "updated": r[7]} for r in rows]
        return aiohttp.web.json_response({"available": len(data) > 0,
                                          "count": len(data), "files": data})
    except Exception as exc:
        return aiohttp.web.json_response({"available": False, "message": str(exc)}, status=500)


def _static(request):
    name = request.match_info.get("name", "index.html")
    # protect path traversal
    if "/" in name or ".." in name:
        name = "index.html"
    path = Path(STATIC_DIR) / name
    if not path.is_file():
        path = Path(STATIC_DIR) / "index.html"
    ctype = "text/html; charset=utf-8"
    if name.endswith(".js"):
        ctype = "application/javascript"
    elif name.endswith(".css"):
        ctype = "text/css"
    elif name.endswith(".json"):
        ctype = "application/json"
    elif name.endswith(".png"):
        ctype = "image/png"
    elif name.endswith(".svg"):
        ctype = "image/svg+xml"
    elif name.endswith(".webmanifest"):
        ctype = "application/manifest+json"
    return aiohttp.web.FileResponse(path, headers={"Content-Type": ctype})


async def build_app():
    app = aiohttp.web.Application()
    app.router.add_get("/api/sessions", api_sessions)
    app.router.add_post("/api/sessions/{id}/spool", api_sessions_attach)
    app.router.add_put("/api/sessions/{id}/manual", api_manual_create)
    app.router.add_post("/api/sessions/{id}/status", api_set_status)
    app.router.add_post("/api/sessions/{id}/gcode", api_sessions_link_gcode)
    app.router.add_post("/api/gcode/analyze", api_analyze_gcode)
    app.router.add_get("/api/spools", api_spools)
    app.router.add_get("/api/k1/files", api_k1_files)
    app.router.add_get("/api/health", api_health)
    app.router.add_get("/api/stats", api_stats)
    app.router.add_get("/", _static)
    app.router.add_get("/thumbs/{tid}", api_thumb)
    app.router.add_get("/{name}", _static)
    return app


tracker = Tracker()


async def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    init_db()
    log.info("Collecteur K1SE démarré — %s — base %s", K1_URL, DB_PATH)

    client = K1Client(K1_URL, tracker.handle)
    app = await build_app()
    runner = aiohttp.web.AppRunner(app)
    await runner.setup()
    site = aiohttp.web.TCPSite(runner, "0.0.0.0", WEB_PORT)
    await site.start()
    log.info("Interface web/API sur :%s", WEB_PORT)

    await asyncio.gather(
        asyncio.create_task(client.run()),
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass