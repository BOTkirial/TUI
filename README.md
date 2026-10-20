# TUI Tools — client HTTP, client SQL & gestion Docker (projet unique)

Trois applications terminal (TUI) en **Python + [Textual](https://textual.textualize.io/)**,
réunies dans un seul projet et une seule image Docker. Textual gère nativement
la **souris**, la **sélection de texte**, le **curseur** et la **copie
presse-papier** (OSC 52, marche via SSH/Docker). La sélection est copiée
**automatiquement** au relâchement de la souris, comme dans Claude Code.

```
tui-tools/
├── Dockerfile            # image unique (les trois TUI)
├── docker-compose.yml    # conteneur TUI
├── Makefile              # raccourcis make up / http / sql
├── http / sql / dock     # lanceurs (à exécuter depuis le terminal de Zed)
├── pyproject.toml        # packaging : commandes http-tui / sql-tui / docker-tui
├── workspace/            # fichiers partagés hôte <-> conteneur (édite-les dans Zed)
│   ├── http/             # requests/ (1 fichier = 1 requête), capture.har, fuzz/
│   └── sql/              # tes .env de connexion
├── common/tui_common/    # briques partagées (copie auto, .env, app de base)
├── http-client/httptui/  # code du client HTTP
├── sql-client/sqltui/    # code du client SQL
└── docker-client/dockertui/  # code du client Docker
```

## Démarrage (« up et basta »)

```bash
docker compose up -d        # construit l'image + lève le conteneur TUI
```

Puis, **depuis le terminal intégré de Zed** (ouvert à la racine du projet) :

```bash
./http      # ouvre le client HTTP
./sql       # ouvre le client SQL
./dock      # ouvre le client Docker
```

Ces lanceurs démarrent la stack si besoin, puis entrent dans le conteneur via
`docker compose exec`. Équivalents `make` : `make http`, `make sql`, `make docker`, `make down`.

> **Pourquoi `exec` et pas `docker compose up` seul ?** Un `up` multiplexe les
> logs de tous les services et ne câble pas un vrai TTY sur une TUI précise.
> On garde donc le conteneur `tui` vivant et on y entre à la demande — c'est ce
> que font `./http` et `./sql`.

### Le workspace partagé

Le dossier `workspace/` est monté dans le conteneur sur `/work`. Tu édites
`workspace/http/requests/*.http` **dans Zed**, et la TUI
qui tourne dans le terminal de Zed voit les fichiers en direct. Les deux TUI
lisent par défaut :

- HTTP → `workspace/http/requests/` (un `.http` par requête)
- SQL  → `workspace/sql/` (fichiers `.env`)

## Sans Docker

### Installation avec pip

Python ≥ 3.10 requis. Installe les commandes `http-tui`, `sql-tui` et `docker-tui` :

```bash
pip install git+https://github.com/BOTkirial/TUI.git
# mise à jour
pip install --upgrade git+https://github.com/BOTkirial/TUI.git
```

> Sur Debian/Ubuntu récents, `pip` refuse d'installer hors environnement virtuel
> (« externally-managed-environment ») : utiliser plutôt
> `pipx install git+https://github.com/BOTkirial/TUI.git` (environnement isolé,
> commandes ajoutées dans `~/.local/bin`).

Les TUI lisent le **répertoire courant** : `http-tui` lit `./requests/` (et
`./fuzz/`), `sql-tui` cherche les `.env` dans `.` ; surchargeables via `HTTP_ROOT`,
`HTTP_REQUESTS_DIR`, `FUZZ_DIR`, `SQL_ROOT`. `docker-tui` utilise le CLI `docker`
(et le plugin `compose`) de la machine.

### Dev local

```bash
pip install -e .            # installe les commandes en mode éditable
cd workspace/http && http-tui
cd workspace/sql  && sql-tui
```

## Raccourcis communs

| Touche         | Action                               |
|----------------|--------------------------------------|
| souris         | sélection → **copie auto**           |
| `ctrl+shift+c` | copier la sélection                  |
| `ctrl+q`       | quitter                              |

---

## Client HTTP

- Requêtes dans **`workspace/http/requests/`** : un fichier `.http` par requête
  (nom du fichier = nom affiché), **sous-dossiers affichés en arbre**.
  « Nouvelle » demande `dossier/nom`, `ctrl+s` sauve, `F5` relit le disque
  (fichiers modifiés dans un éditeur — écrase les modifications non sauvées).
- **Écran d'accueil « Requêtes »** au lancement : arbre filtrable (sur `dossier/nom`,
  plusieurs mots possibles ; ↑↓ pour choisir, Entrée pour ouvrir, Échap pour vider),
  boutons Nouvelle (`ctrl+n`) / Ouvrir / Import curl (`ctrl+o`) / Actualiser (`F5`).
- **Écran de la requête** : édition, envoi (`ctrl+r`), fuzzing (`ctrl+f`), sauvegarde
  (`ctrl+s`). Échap revient à la liste (avertit une fois si des modifications ne sont
  pas sauvées ; les résultats de fuzzing ne sont pas conservés).
- **Import curl** (« Copier en tant que cURL ») → crée le `.http`, **credentials conservés**.
- **Fuzzing** via balises + listes lues dans `workspace/http/fuzz/`.

### Syntaxe `.http`

| Balise            | Rôle                                          |
|-------------------|-----------------------------------------------|
| `{{fuzz:liste}}`  | point de fuzzing (double accolade → pas de collision avec le JSON) |

Les listes sont les fichiers `*.txt` de `workspace/http/fuzz/` (surchargeable
via `FUZZ_DIR`) : `sqli.txt` → `{{fuzz:sqli}}`. Une valeur par ligne, espaces
conservés, lignes vides ignorées, `<empty>` pour la chaîne vide. Le répertoire
est relu à chaque envoi : une liste ajoutée dans Zed est utilisable sans relancer.
Fournies : `numbers`, `ascii`, `special`, `sqli`, `xss`, `bool`, `empty`, `wordlist`.
Plusieurs balises → produit cartésien (borné à 500).

### Import curl

Dans le navigateur (onglet Réseau) : clic droit sur la requête → **« Copier en
tant que cURL (bash) »** (Firefox : « Copier comme cURL (POSIX) »). Puis bouton
**« Import curl »** / `ctrl+o`, coller dans la zone, choisir `dossier/nom`
(pré-rempli avec le dossier sélectionné ; laisser `dossier/` = nom déduit de l'URL).

Gérés : `-X`, `-H`, `-b` (→ `Cookie`), `-d`/`--data-raw`/`--data-binary`/`--json`,
`-u` (→ Basic), `-A`, `-e`, `-G`, `-I`, guillemets `$'…'` de Chrome. Ignorés :
`--compressed`, `-k`, `-L`… ; `-F` (multipart) non géré. `Host`/`Content-Length`
retirés. Le format Windows `cmd` (avec `^`) n'est pas supporté.
Credentials (`Authorization`, `Cookie`) conservés **en clair** dans le `.http`.

### Résultats de fuzzing (onglet « Fuzzing »)

- **Clic sur un en-tête = tri** (re-clic inverse ; `↑`/`↓`), numérique sur statut/temps/taille.
- **Champ de recherche** filtrant sur le **corps de la réponse**, **debouncé** (250 ms).
- **Clic sur une ligne** = détail complet de la réponse (URL, statut, en-têtes, corps),
  mapping conservé même après tri/filtre.

Raccourcis — liste : `ctrl+n` nouvelle · `ctrl+o` import curl · `F5` actualiser · requête : `ctrl+r` envoyer · `ctrl+f` fuzzer · `ctrl+s` sauver · Échap retour.

---

## Client SQL

- **Écran d'accueil « Connexions »** au lancement : choisis ou crée une connexion,
  puis « Connecter » (ou Entrée) ouvre l'écran de requêtes. Échap y revient.
- **Nouvelle connexion** : bouton « Nouvelle » → formulaire (nom, driver, hôte,
  port, base, utilisateur, mot de passe). Stockées dans le volume `tui-config`.
- **Import depuis DBeaver** (métadonnées + tentative de déchiffrement local).
- **Lecture `.env`** (`DATABASE_URL` ou `DB_HOST`/`DB_PORT`/…).
- Moteurs : **PostgreSQL**, **MySQL**, **MariaDB**.

Dans la TUI, bouton **« Import .env »** : les connexions décrites par les `.env`
de `workspace/sql/` sont ajoutées. Clic sur une
table → `SELECT * … LIMIT 100`. `ctrl+r` exécute (la sélection si présente).

### Éditer les résultats

`Entrée` (ou `F2`) sur une cellule ouvre une fenêtre d'édition : `Entrée` valide,
`shift+Entrée` insère un saut de ligne, bouton **NULL** pour vider, Échap annule.
Les cellules modifiées sont surlignées en jaune ; **`ctrl+s`** enregistre tout dans
une seule transaction (une erreur ou une ligne introuvable = rien n'est écrit).

Une colonne est modifiable si elle vient directement d'une table dont **toute la
clé primaire figure dans le résultat** (alias et jointures acceptés ; colonnes
calculées, vues sans clé, tables sans clé primaire et valeurs binaires = lecture
seule). Relancer une requête ou quitter l'écran avec des modifications en attente
demande de recommencer pour confirmer l'abandon.

Copier une cellule : **double-clic** dessus, ou `ctrl+shift+c` sur la cellule courante.

### Import DBeaver

Décommente le montage `DBeaverData` dans `docker-compose.yml`, puis bouton
**« Import DBeaver »**. Le déchiffrement des mots de passe porte sur **tes**
identifiants locaux ; s'il échoue, seules les métadonnées sont importées.

---

## Client Docker

Pilote le **Docker de l'hôte** : le conteneur `tui` monte `/var/run/docker.sock`
(⚠ accès équivalent root sur la machine, pour tout ce qui tourne dans ce conteneur)
et embarque le CLI `docker` + le plugin `compose`. Un seul écran, quatre onglets
(clic, ou `ctrl+PgUp`/`ctrl+PgDn`), chacun avec un champ filtre (mêmes gestes que
les autres TUI : frappe redirigée, ↑↓ depuis le champ, Échap pour vider) :

- **Conteneurs** : arbre par **stack docker compose** (+ « hors compose »), détail
  du nœud courant à droite (ports, réseaux, montages). Sur une stack : **Up** (`F6`,
  `compose up -d`), **Stop** (`F7`), **Down** (`F8`, confirmé, volumes conservés).
  Sur un conteneur : start / stop / suppression.
- **Images** : dépôt, tag, taille, conteneurs qui l'utilisent ; `Suppr` supprime
  (le tag seul si l'image en a plusieurs ; refusé par Docker si un conteneur l'utilise).
- **Réseaux** : pilote, portée, conteneurs connectés ; suppression (sauf `bridge`/`host`/`none`).
- **Volumes** : stack, **chemin sur disque**, conteneurs qui le montent et **point
  de montage** ; suppression.

`F5` actualise, double-clic (ou `ctrl+shift+c`) copie la cellule courante.
`compose up` relit les fichiers compose à leur **chemin hôte** : `$HOME` est monté
au même chemin (lecture seule) : une stack dont les fichiers sont hors de `$HOME`
ne peut pas être relancée (`up`) depuis la TUI, Stop/Down fonctionnent toujours.
Les stacks qui construisent leurs images (`build:`) doivent avoir été construites
depuis l'hôte (buildx n'est pas embarqué). Attention : `Down`
sur la stack `tui-tools` arrête la TUI elle-même.
# TUI
# TUI
