# shorts — des captures de Kora aux shorts verticaux

Transforme un enregistrement d'écran (OBS, GNOME, etc., en 16:9) en short 1080×1920 prêt
pour TikTok, Reels et YouTube Shorts :

- **montage** : on garde des segments, on coupe les temps morts, on accélère les attentes
  (étiquette « ▶▶ ×4 » automatique) ;
- **caméra** : zooms et panoramiques fluides (easing, sous-pixel) vers la zone qui compte :
  le panneau, l'orbe, une confirmation ;
- **mise en page** : « cadre » (carte 4:5 arrondie avec ombre, sur fond flouté de la capture)
  ou « plein » (9:16) ; accroche en haut, barre de progression, textes posés ;
- **sous-titres karaoké** : Whisper en local, mot par mot, mot prononcé en couleur, placés
  hors des zones masquées par l'interface des applis ;
- **floutage** des zones privées (chemins, courriels, notifications), appliqué avant tout le
  reste, y compris au fond flouté ;
- **son** : musique baissée automatiquement quand quelqu'un parle, loudness normalisée à
  -14 LUFS en deux passes ;
- **export** : H.264 High, CRF 18, yuv420p, BT.709, AAC 192k 48 kHz, `+faststart`.

## Installation

```bash
sudo apt install ffmpeg          # avec libass (c'est le cas du paquet Ubuntu/Debian)
cd shorts
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
```

Python 3.11 minimum. Au premier rendu avec sous-titres, Whisper télécharge son modèle depuis
huggingface.co (~500 Mo pour `small`), ensuite tout est local.

## Flux de travail

```bash
# 1. Proposer un plan : coupe les passages figés ET silencieux,
#    propose ×3 sur les longs silences où l'écran bouge
python shorts.py analyser captures/kora-courrier.mkv -o plans/courrier.toml

# 2. Régler le cadrage image par image (PNG avec titre et sous-titres)
python shorts.py rendre plans/courrier.toml --image 00:12

# 3. Vérifier les sous-titres, corriger plans/courrier.mots.json si besoin
python shorts.py transcrire plans/courrier.toml

# 4. Aperçu rapide (540×960), puis rendu final
python shorts.py rendre plans/courrier.toml --apercu
python shorts.py rendre plans/courrier.toml
```

Tous les temps du plan sont ceux de la **capture** (ce que tu lis dans ton lecteur vidéo) ; le
script les convertit lui-même en temps du short, coupes et accélérations comprises. Toutes les
options sont commentées dans [`plans/exemple.toml`](plans/exemple.toml).

La transcription est gardée dans `<plan>.mots.json` : corrige le texte des mots, garde les
temps, elle est reprise telle quelle au rendu suivant. Si tu changes les segments, le son du
short change et la transcription est refaite (l'ancien fichier est remplacé : garde une copie
si tu y as passé du temps).

## Conseils de capture

- Enregistre en 1440p ou plus si possible : un zoom sur le panneau de conversation d'une
  capture 1080p reste lisible, mais une capture plus grande donne un texte plus net.
- Garde la souris immobile hors du geste montré : un curseur qui bouge empêche `analyser` de
  détecter les temps morts.
- Les clés `vocabulaire` et `modele_whisper = "medium"` règlent la plupart des fautes sur
  « Kora » et « Choragos ».

## Ce qui a été vérifié

Sur une capture de test de 1920×1080 (page web enregistrée par Chromium + voix de synthèse
espeak) : `analyser` → plan, rendu final et aperçu dans les deux dispositions, segments
accélérés avec et sans son, floutage, texte posé, sous-titres karaoké depuis un
`.mots.json`, `--image`, loudness mesurée à -14,0 LUFS sur la sortie. Les erreurs de plan
(clé inconnue, zone hors capture, segments qui se chevauchent, temps ou couleur illisible) sont
refusées avec un message clair.

**Pas encore vérifié** : la transcription Whisper elle-même (le modèle n'a pas pu être
téléchargé dans l'environnement de développement) et une vraie capture de Choragos.
