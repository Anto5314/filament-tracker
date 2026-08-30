# 🧵 Filament Tracker

Suivi de filament et journal d'impressions pour **imprimante 3D Creality K1SE** (CrealityOS) — avec interface web PWA (mobile-friendly).

Le collecteur se connecte au **WebSocket propriétaire (port 9999)** de la K1SE, suit les impressions **en direct**, importe **l'historique complet**, télécharge les **miniatures** des modèles, et se synchronise avec **Spoolman** pour le décompte du filament.

## ✨ Fonctionnalités

- 📋 **Journal automatique** des impressions (statut, durée, filament réellement consommé)
- 📜 **Import rétroactif de l'historique** de l'imprimante (toutes les impressions passées, terminées ET arrêtées)
- 🖼️ **Miniatures des modèles** — téléchargées depuis l'imprimante, visibles dans l'onglet « Imprimante »
- ⚖️ **Quantité de filament par modèle** (champ `filamentWeight` fourni par le slicer)
- 🧵 **Association bobine → impression** avec décompte automatique dans Spoolman
- 🪙 **Option « associer sans décompter »** pour les bobines pesées à la main (évite le double comptage)
- 📱 **PWA** installable sur mobile
- 🖥️ **Mock K1** inclus pour tester sans l'imprimante

## 🏗️ Architecture

| Service | Rôle | Port |
|---|---|---|
| `collector` | Connexion WS à l'imprimante, journal, API, interface web | 8123 |
| `spoolman` | Inventaire des bobines, QR codes, poids restant | 7912 |

## 🚀 Installation

```bash
# 1. Récupérer le code
git clone https://github.com/Anto5314/filament-tracker.git
cd filament-tracker

# 2. Configurer l'adresse de l'imprimante
echo "K1_HOST=192.168.1.100" > .env
echo "K1_PORT=9999" >> .env

# 3. Lancer
docker compose up -d --build
```

Puis ouvrir :
- Interface web : **http://<serveur>:8123**
- Spoolman : **http://<serveur>:7912**

## ⚙️ Configuration

Variables d'environnement (fichier `.env` ou `docker-compose.yml`) :

| Variable | Défaut | Description |
|---|---|---|
| `K1_HOST` | `192.168.1.41` | Adresse IP de l'imprimante K1SE |
| `K1_PORT` | `9999` | Port WebSocket CrealityOS |
| `K1_SUBPROTOCOL` | *(vide)* | Sous-protocole WS (inutilisé sur K1SE) |
| `DB_PATH` | `/data/k1_sessions.db` | Base SQLite locale |
| `SPOOLMAN_URL` | `http://spoolman:8000` | URL API Spoolman |
| `WEB_PORT` | `8123` | Port de l'interface |
| `POLL_INTERVAL` | `5` | Intervalle de requêtes WS (secondes) |
| `K1_SILENCE_PROBE_SECS` | `8` | Délai avant sonde de réveil (watchdog) |
| `K1_SILENCE_DEAD_SECS` | `30` | Délai avant reconnexion forcée |

## 🔌 Protocole K1SE (reverse engineering)

La K1SE tourne sous **CrealityOS** (Klipper **sans Moonraker**) — il n'y a pas de REST complet pour les fichiers. Le collecteur utilise :

- **WebSocket port 9999** : push d'état en continu + requêtes `ReqPrinterPara`, `reqPrintObjects`, et la combinaison `reqGcodeFile + reqGcodeList + reqHistory + reqElapseVideoList + reqPrintObjects + reqMaterialBoxsInfo` (la liste des fichiers n'est renvoyée qu'avec cette combinaison **complète**).
- **HTTP port 80** : téléchargement des miniatures via `GET /downloads/humbnail/<fichier>.png` (attention : le firmware écrit « **humb**nail » et non « thumbnail ») et des .gcode via `/downloads/gcode/<fichier>.gcode`.
- **État** : payload racine avec `state` entier (0=idle, 1=printing, 2=completed, 3=error, 4=aborted, 5=paused), `printFileName`, `usedMaterialLength`, `nozzleTemp`/`bedTemp` (chaînes), `err.errcode`, `model`.
- L'imprimante envoie des pushs **incrémentaux** (champs isolés) → le collecteur fait une **fusion progressive** de l'état.
- **Watchdog de silence** : si la K1 ne répond plus (éteinte, veille), la connexion est fermée après 30 s de silence et relancée toutes les 10 s.

### Historique (`historyList`)

Chaque job K1 contient : `id` unique, `filename`, `starttime`, `usagetime` (s), `usagematerial` (mm), `printfinish` (1 = terminé, 0 = arrêté), `thumbnail`. L'import est **idempotent** (hachage `id|filename`) et **fusionne** avec les sessions déjà créées en direct (pas de doublons).

## 🖼️ Captures

*(à venir — ou regarder directement l'interface sur http://<serveur>:8123)*

## 🛠️ Développement

```bash
# Tester sans l'imprimante : lancer le mock sur 9999
python3 mock_k1.py 9999 demo_vase.gcode

# Lancer le collecteur en local (hors Docker)
K1_HOST=127.0.0.1 K1_PORT=9999 SPOOLMAN_URL=http://127.0.0.1:8000 python3 collector.py
```

## 📄 Licence

MIT — voir [LICENSE](LICENSE).