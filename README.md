# 🧵 Filament Tracker

> Suivi de filament et journal d'impressions pour **imprimante 3D Creality K1SE** — interface web PWA, synchronisation automatique avec Spoolman.

**🇬🇧 English version: [README.en.md](README.en.md)**

Filament Tracker se connecte au **WebSocket propriétaire (port 9999)** de la K1SE (firmware CrealityOS), suit les impressions **en temps réel**, importe **l'historique complet** des impressions passées, **télécharge les miniatures** des modèles, et se synchronise avec **Spoolman** pour gérer le décompte du filament sur chaque bobine.

---

## ✨ Pourquoi Filament Tracker ?

Ta K1SE tourne sous **CrealityOS** (Klipper **sans Moonraker**) : il n'existe aucun moyen simple de savoir ce qui a été imprimé, combien de filament a été consommé, et quelles impressions ont échoué. Filament Tracker comble ce manque :

- **Tu imprimes → le journal se remplit automatiquement** (nom du fichier, statut, filament réellement consommé)
- **Toutes tes impressions passées sont importées** depuis l'historique de l'imprimante (terminées **et** arrêtées)
- **Tu vois tes modèles avec leur miniature** dans un onglet « Imprimante »
- **Tu associes une impression à une bobine** → le poids restant se décompte automatiquement dans Spoolman

---

## ✨ Fonctionnalités

| 🎯 | Fonctionnalité | Détails |
|---|---|---|
| 📋 | **Journal automatique** | Chaque impression démarre une session : fichier, heure, statut (terminée / arrêtée / erreur), durée, filament mesuré (`usedMaterialLength`) |
| 📜 | **Import rétroactif** | L'historique complet de l'imprimante (`historyList`) est importé automatiquement — toutes les impressions passées apparaissent dans le journal |
| 🖼️ | **Miniatures des modèles** | Téléchargées depuis l'imprimante (`/downloads/humbnail/*.png` — oui, avec la faute de frappe du firmware !) et affichées dans l'onglet « Imprimante » |
| ⚖️ | **Quantité par modèle** | Le champ `filamentWeight` du slicer est lu pour chaque fichier : matière + grammes estimés + durée d'impression |
| 🧵 | **Association bobine → impression** | Depuis le journal ou l'onglet Imprimante, choisis la bobine utilisée → décompte automatique dans Spoolman |
| 🪙 | **« Associer sans décompter »** | Pour les bobines **pesées à la main** : les anciennes impressions ne doivent pas re-décompter leur filament (évite le double comptage). Case à cocher, décochée par défaut pour les sessions historiques |
| 📱 | **PWA mobile** | Installable sur le téléphone (écran d'accueil), interface optimisée mobile |
| 🖥️ | **Mock K1 inclus** | Un simulateur d'imprimante (`mock_k1.py`) permet de tester toute l'appli sans toner abîmer ta machine |
| 🔄 | **Reconnexion automatique** | Watchdog de silence : si l'imprimante est éteinte ou en veille, le collecteur se reconnecte toutes les 10 s |
| 🔒 | **Aucun doublon** | L'import d'historique est idempotent et fusionne avec les sessions créées en direct |

---

## 🏗️ Architecture

```
┌─────────────────────────────┐        ┌──────────────────────┐
│  Creality K1SE (port 9999) │  WS   │  filament-tracker     │
│  - état temps réel         │ ─────▶ │  collector.py         │
│  - liste fichiers          │        │  - journal            │
│  - historique              │        │  - API web :8123      │
│  - miniatures (HTTP :80)   │ ─────▶ │  - déduplication      │
└─────────────────────────────┘        └──────────┬───────────┘
                                                  │ REST
                                                  ▼
                                       ┌──────────────────────┐
                                       │  Spoolman (port 7912)│
                                       │  bobines, poids rest.│
                                       └──────────────────────┘
```

| Service | Rôle | Port |
|---|---|---|
| `collector` | Connexion WS à l'imprimante, journal, API, interface PWA | 8123 |
| `spoolman` | Inventaire des bobines, QR codes, poids restant | 7912 |

---

## 🚀 Installation

### Prérequis
- Docker + Docker Compose
- Une imprimante Creality K1SE accessible sur le réseau local
- (Optionnel) Spoolman déjà en place — le docker-compose le déploie automatiquement

### Étapes

```bash
# 1. Récupérer le code
git clone <votre-url-repo>
cd filament-tracker

# 2. Configurer l'adresse de l'imprimante
echo "K1_HOST=192.168.1.100" > .env
echo "K1_PORT=9999" >> .env

# 3. Lancer
docker compose up -d --build
```

Puis ouvrir :
- **Interface web** : http://<serveur>:8123
- **Spoolman** : http://<serveur>:7912

---

## ⚙️ Configuration

Variables d'environnement (fichier `.env` ou `docker-compose.yml`) :

| Variable | Défaut | Description |
|---|---|---|
| `K1_HOST` | `192.168.1.41` | Adresse IP de l'imprimante K1SE |
| `K1_PORT` | `9999` | Port WebSocket CrealityOS |
| `K1_SUBPROTOCOL` | *(vide)* | Sous-protocole WS (inutilisé sur K1SE, laissé pour compatibilité) |
| `DB_PATH` | `/data/k1_sessions.db` | Base SQLite locale |
| `SPOOLMAN_URL` | `http://spoolman:8000` | URL API Spoolman |
| `WEB_PORT` | `8123` | Port de l'interface web |
| `POLL_INTERVAL` | `5` | Intervalle des requêtes WS (secondes) |
| `K1_SILENCE_PROBE_SECS` | `8` | Délai avant sonde de réveil (watchdog) |
| `K1_SILENCE_DEAD_SECS` | `30` | Délai avant reconnexion forcée |

---

## 📖 Utilisation

1. **Ajoute tes bobines dans Spoolman** (http://<serveur>:7912) avec leur poids mesuré
2. **Ouvre l'interface** (http://<serveur>:8123)
3. **Tablature 📋 Journal** : toutes les impressions (passées + futures) avec statut et filament
4. **Tablature 📁 Imprimante** : les modèles présents sur l'imprimante avec miniature + quantité + durée
5. **Clique sur une session ou un modèle** → choisis la bobine → le poids se décompte

> 💡 **Astuce mobile** : sur Android/iOS, « Ajouter à l'écran d'accueil » installe Filament Tracker comme une appli.

---

## 🔌 Protocole K1SE (reverse engineering)

La K1SE n'a **pas d'API REST complète** pour les fichiers (contrairement aux printers Klipper avec Moonraker). Le protocole CrealityOS a été entièrement compris par tests sur une machine réelle :

### WebSocket (port 9999)
- **Push** : l'imprimante envoie son état en continu (`state`, `printFileName`, `usedMaterialLength`, `nozzleTemp`, `bedTemp`, `err.errcode`, ...)
- **Requêtes** : `ReqPrinterPara`, `reqPrintObjects`
- **Liste des fichiers + historique** : la combinaison **complète et obligatoire** :
  ```json
  {"method":"get","params":{"reqGcodeFile":1,"reqGcodeList":1,"reqHistory":1,
    "reqElapseVideoList":1,"reqPrintObjects":1,"reqMaterialBoxsInfo":1}}
  ```
  → réponse `retGcodeFileInfo2` (fichiers avec miniatures + `filamentWeight`) + `historyList` (jobs passés).
  > ⚠️ `reqGcodeList` **seul** ne renvoie rien ! La K1 ignore les demandes partielles.

### HTTP (port 80)
- Miniature : `GET /downloads/humbnail/<fichier-sans-.gcode>.png` → PNG 96×96
  > ⚠️ Le firmware écrit **`humbnail`** (faute de frappe officielle de Creality) — pas « thumbnail ».
- Gcode complet : `GET /downloads/gcode/<fichier>.gcode` *(le collecteur ne télécharge QUE les miniatures, jamais les .gcode complets pour économiser la bande passante)*

### États (`state`)
| Valeur | Signification |
|---|---|
| 0 | Arrêtée / idle |
| 1 | Impression en cours |
| 2 | Terminée (completed) |
| 3 | Erreur (failed) |
| 4 | Abortée |
| 5 | En pause |

### Historique (`historyList`)
Chaque job contient : `id` unique, `filename`, `starttime`, `usagetime` (s), `usagematerial` (mm), `printfinish` (1 = terminé, 0 = arrêté), `thumbnail`.
- **Idempotent** : la session est hachée (`sha1(id|filename)`) → pas de doublon au re-import
- **Fusion live** : si une session en direct existe déjà pour le même fichier/début, l'import la complète au lieu d'en créer une autre

### Watchdog
L'imprimante éteinte brutalement laisse le WebSocket bloqué (aucune erreur levée). Le watchdog détecte le silence (>8 s → sonde, >30 s → fermeture) et relance la connexion toutes les 10 s.

---

## 🛠️ Développement

```bash
# Tester sans l'imprimante : lancer le mock sur 9999
python3 mock_k1.py 9999 demo_vase.gcode

# Lancer le collecteur en local (hors Docker)
K1_HOST=127.0.0.1 K1_PORT=9999 SPOOLMAN_URL=http://127.0.0.1:8000 python3 collector.py
```

---

## 🎯 Compatibilité

Le protocole CrealityOS a été **reverse-engineer et validé sur une K1SE réelle** (firmware `DWIN CR4CU220812S11 1.3.5.22`). Voici ce qu'il faut savoir pour les autres utilisateurs :

| Appareil / firmware | Compatibilité |
|---|---|
| **K1SE** — même firmware | ✅ Fonctionne à 100 % (validé en conditions réelles) |
| **K1SE** — firmware plus récent ou plus ancien | 🟡 Très probablement fonctionnel (les clés WS sont stables chez Creality), mais non garanti |
| **K1 / K1C / K1 Max** | 🟡 Protocole CrealityOS très proche, mais à tester sur votre machine |

**Points pratiques :**
- Le collecteur se connecte au **WebSocket port 9999** — vérifiez que votre imprimante est sur le même réseau local et que le port n'est pas bloqué par le pare-feu.
- Une fois connecté, **tout est automatique** : historique importé, fichiers + miniatures synchronisés, journal mis à jour en direct.
- Si votre firmware ne répond pas à la combinaison de requêtes, ouvrez une **issue GitHub** avec votre version de firmware — nous pourrons l'adapter.

**⚠️ Ce projet n'a aucun lien avec Creality.** Le protocole a été découvert par observation du trafic réseau ; il peut changer à tout moment avec une mise à jour du firmware.

---

## 🧠 Notes importantes

- **Décompte du filament** : le collecteur lit `usagematerial` (mm) envoyé par l'imprimante, le convertit en grammes avec la densité du filament (défaut 1,24 g/cm³ pour le PLA) et décompte `remaining_weight` dans Spoolman.
- **Bobines pesées** : si tu as pesé tes bobines à la balance, utilise la case « 🪙 Décompter » décochée pour les impressions **antérieures** à la pesée — sinon double comptage.
- **Miniatures historiques** : les thumbnails d'historique id-based ne sont pas servis par le firmware → le collecteur rattache la miniature du fichier correspondant.

---

## 🖼️ Captures d'écran

**📊 Dashboard imprimante** — état temps réel (connexion, températures buse/plateau, statistiques globales) :

![Dashboard imprimante](screenshots/dashboard.png)

**📋 Journal des impressions** — chaque impression est enregistrée automatiquement (statut, durée, filament consommé) :

![Journal des impressions](screenshots/journal.png)

**🧵 Bobines & filaments** — inventaire Spoolman (matériau, couleur, poids restant) et bibliothèque d'association :

![Bobines et filaments](screenshots/filaments.png)

**📱 Vue mobile** — interface responsive, installable comme PWA sur téléphone :

![Vue mobile](screenshots/mobile.png)

*Les captures montrent l'interface réelle (adresse IP de démonstration floutée).*

---

## 📄 Licence

MIT — voir [LICENSE](LICENSE).