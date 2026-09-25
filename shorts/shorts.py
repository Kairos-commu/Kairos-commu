#!/usr/bin/env python3
"""shorts.py — captures d'écran de Kora → shorts verticaux 1080×1920 (TikTok, Reels, Shorts).

Trois commandes :

    python shorts.py analyser capture.mkv -o plans/courrier.toml   # propose un plan (coupe les temps morts)
    python shorts.py rendre plans/courrier.toml                     # rendu final
    python shorts.py rendre plans/courrier.toml --apercu            # rendu rapide 540×960
    python shorts.py rendre plans/courrier.toml --image 00:42       # une image PNG pour régler le cadrage
    python shorts.py transcrire plans/courrier.toml                 # sous-titres seuls, à corriger à la main

Tous les temps du plan sont en temps de la CAPTURE (ceux que tu lis dans ton lecteur vidéo) :
le script les convertit lui-même vers le temps du short, coupes et accélérations comprises.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

try:
    import cv2
    import numpy as np
except ImportError:  # pragma: no cover
    sys.exit("Il manque numpy/opencv : pip install -r requirements.txt")

W_REF, H_REF = 1080, 1920  # géométrie de référence ; l'aperçu la divise par deux


# ─── utilitaires ────────────────────────────────────────────────────────────────


def erreur(msg: str) -> None:
    sys.exit(f"✗ {msg}")


def info(msg: str) -> None:
    print(f"· {msg}", flush=True)


def lire_temps(v, nom: str = "temps") -> float:
    """42 | 42.5 | "0:42" | "01:02:03.5" → secondes."""
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str) and re.fullmatch(r"\d+(:\d+){0,2}(\.\d+)?", v.strip()):
        s = 0.0
        for part in v.strip().split(":"):
            s = s * 60 + float(part)
        return s
    erreur(f"{nom} illisible : {v!r} (attendu : 42, 42.5, \"0:42\" ou \"1:02:03.5\")")
    raise AssertionError


def fmt_temps(s: float) -> str:
    m, s = divmod(max(s, 0), 60)
    return f"{int(m):02d}:{s:05.2f}"


def couleur_ass(hexa: str, alpha: int = 0) -> str:
    h = hexa.lstrip("#")
    if not re.fullmatch(r"[0-9a-fA-F]{6}", h):
        erreur(f"couleur illisible : {hexa!r} (attendu #RRGGBB)")
    r, g, b = h[0:2], h[2:4], h[4:6]
    return f"&H{alpha:02X}{b}{g}{r}".upper()


def couleur_bgr(hexa: str) -> tuple[int, int, int]:
    h = hexa.lstrip("#")
    return int(h[4:6], 16), int(h[2:4], 16), int(h[0:2], 16)


def verifier_outils() -> None:
    for outil in ("ffmpeg", "ffprobe"):
        if not shutil.which(outil):
            erreur(f"{outil} introuvable dans le PATH (sudo apt install ffmpeg)")
    filtres = subprocess.run(["ffmpeg", "-hide_banner", "-filters"], capture_output=True, text=True).stdout
    if not re.search(r"\sass\s", filtres):
        erreur("ton ffmpeg n'a pas libass (filtre « ass ») : les sous-titres ne peuvent pas être incrustés")


def executer(cmd: list[str], quoi: str) -> str:
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        print(p.stderr[-3000:], file=sys.stderr)
        erreur(f"{quoi} a échoué (ffmpeg code {p.returncode})")
    return p.stderr


@dataclass
class Media:
    largeur: int
    hauteur: int
    duree: float
    a_du_son: bool


def sonder(chemin: Path) -> Media:
    p = subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(chemin)],
        capture_output=True, text=True,
    )
    if p.returncode != 0:
        erreur(f"ffprobe ne lit pas {chemin} : {p.stderr.strip()}")
    d = json.loads(p.stdout)
    video = next((s for s in d["streams"] if s["codec_type"] == "video"), None)
    if video is None:
        erreur(f"{chemin} ne contient pas de piste vidéo")
    audio = any(s["codec_type"] == "audio" for s in d["streams"])
    duree = float(d["format"].get("duration") or video.get("duration") or 0)
    # rotation éventuelle (captures de téléphone)
    rot = 0
    for sd in video.get("side_data_list", []):
        rot = int(sd.get("rotation", 0) or 0)
    w, h = int(video["width"]), int(video["height"])
    if abs(rot) in (90, 270):
        w, h = h, w
    return Media(w, h, duree, audio)


# ─── le plan ────────────────────────────────────────────────────────────────────


@dataclass
class Segment:
    debut: float
    fin: float
    vitesse: float = 1.0
    son: bool = True
    etiquette: str = ""
    # calculés
    f0: int = 0  # première image du short
    f1: int = 0  # image de fin (exclue)


@dataclass
class Cle:
    t: float  # temps capture
    zone: tuple[float, float, float, float]  # x, y, w, h en pixels de la capture
    transition: float


@dataclass
class Flou:
    debut: float
    fin: float
    zone: tuple[int, int, int, int]


@dataclass
class Texte:
    debut: float
    fin: float
    texte: str
    position: str


@dataclass
class Plan:
    chemin: Path
    source: Path
    sortie: Path
    media: Media
    disposition: str
    fps: int
    titre: str
    titre_duree: float
    langue: str
    modele_whisper: str
    vocabulaire: str
    sous_titres: bool
    mots_par_groupe: int
    majuscules: bool
    police: str
    dossier_polices: str
    accent: str
    musique: str
    musique_db: float
    barre_progression: bool
    lufs: float
    segments: list[Segment] = field(default_factory=list)
    cles: list[Cle] = field(default_factory=list)
    flous: list[Flou] = field(default_factory=list)
    textes: list[Texte] = field(default_factory=list)

    @property
    def nb_images(self) -> int:
        return self.segments[-1].f1

    @property
    def duree(self) -> float:
        return self.nb_images / self.fps

    def vers_short(self, t: float) -> float:
        """Temps capture → temps short. Un instant coupé est ramené au début du segment suivant."""
        for s in self.segments:
            if t < s.debut:
                return s.f0 / self.fps
            if t <= s.fin:
                return s.f0 / self.fps + (t - s.debut) / s.vitesse
        return self.duree

    def vers_capture(self, i: int) -> float:
        """Numéro d'image du short → temps capture."""
        for s in self.segments:
            if i < s.f1:
                return s.debut + (i - s.f0) / self.fps * s.vitesse
        s = self.segments[-1]
        return s.fin

    def empreinte_montage(self) -> str:
        """Change dès que le son du short change : invalide une transcription périmée."""
        brut = json.dumps([(s.debut, s.fin, s.vitesse, s.son) for s in self.segments] + [str(self.source)])
        return hashlib.sha1(brut.encode()).hexdigest()[:12]


def lire_zone(v, media: Media, ou: str) -> tuple[float, float, float, float]:
    if v == "tout":
        return (0.0, 0.0, float(media.largeur), float(media.hauteur))
    if not (isinstance(v, list) and len(v) == 4 and all(isinstance(n, (int, float)) for n in v)):
        erreur(f"{ou} : zone attendue [x, y, largeur, hauteur] ou \"tout\", reçu {v!r}")
    x, y, w, h = (float(n) for n in v)
    if w <= 0 or h <= 0:
        erreur(f"{ou} : largeur et hauteur doivent être positives")
    if x < 0 or y < 0 or x + w > media.largeur + 1 or y + h > media.hauteur + 1:
        erreur(f"{ou} : zone {v} hors de la capture ({media.largeur}×{media.hauteur})")
    return x, y, w, h


CLES_CONNUES = {
    "source", "sortie", "disposition", "fps", "titre", "titre_duree", "langue", "modele_whisper",
    "vocabulaire", "sous_titres", "mots_par_groupe", "majuscules", "police", "dossier_polices",
    "couleur_accent", "musique", "musique_db", "barre_progression", "lufs", "transition",
    "segment", "cadrage", "flou", "texte",
}


def charger_plan(chemin: Path) -> Plan:
    try:
        d = tomllib.loads(chemin.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as e:
        erreur(f"{chemin} : TOML invalide — {e}")
    inconnues = set(d) - CLES_CONNUES
    if inconnues:
        erreur(f"{chemin} : clé(s) inconnue(s) {sorted(inconnues)} — faute de frappe ?")
    base = chemin.parent
    if "source" not in d:
        erreur(f"{chemin} : « source » manquant")
    source = (base / d["source"]).resolve()
    if not source.exists():
        erreur(f"capture introuvable : {source}")
    media = sonder(source)
    sortie = (base / d.get("sortie", f"../sorties/{chemin.stem}.mp4")).resolve()

    disposition = d.get("disposition", "cadre")
    if disposition not in ("cadre", "plein"):
        erreur("disposition : « cadre » ou « plein »")

    p = Plan(
        chemin=chemin, source=source, sortie=sortie, media=media, disposition=disposition,
        fps=int(d.get("fps", 30)),
        titre=str(d.get("titre", "")),
        titre_duree=float(d.get("titre_duree", 0)),
        langue=str(d.get("langue", "fr")),
        modele_whisper=str(d.get("modele_whisper", "small")),
        vocabulaire=str(d.get("vocabulaire", "Kora, Choragos, Ollama, Florent.")),
        sous_titres=bool(d.get("sous_titres", True)),
        mots_par_groupe=int(d.get("mots_par_groupe", 3)),
        majuscules=bool(d.get("majuscules", True)),
        police=str(d.get("police", "DejaVu Sans")),
        dossier_polices=str((base / d["dossier_polices"]).resolve()) if d.get("dossier_polices") else "",
        accent=str(d.get("couleur_accent", "#FFD400")),
        musique=str((base / d["musique"]).resolve()) if d.get("musique") else "",
        musique_db=float(d.get("musique_db", -20)),
        barre_progression=bool(d.get("barre_progression", True)),
        lufs=float(d.get("lufs", -14)),
    )
    couleur_ass(p.accent)  # valide
    if p.musique and not Path(p.musique).exists():
        erreur(f"musique introuvable : {p.musique}")

    # segments
    brut = d.get("segment") or [{"debut": 0, "fin": media.duree}]
    for n, s in enumerate(brut, 1):
        ou = f"segment n°{n}"
        debut = lire_temps(s.get("debut", 0), f"{ou}.debut")
        fin = min(lire_temps(s.get("fin", media.duree), f"{ou}.fin"), media.duree)
        vitesse = float(s.get("vitesse", 1))
        if not 0.25 <= vitesse <= 50:
            erreur(f"{ou} : vitesse {vitesse} hors de [0.25, 50]")
        if fin <= debut:
            erreur(f"{ou} : fin ({fin}) avant début ({debut}) — ou au-delà de la capture ({media.duree:.2f} s)")
        son = bool(s.get("son", vitesse == 1))
        etiquette = s.get("etiquette", f"▶▶ ×{vitesse:g}" if vitesse > 1 else "")
        p.segments.append(Segment(debut, fin, vitesse, son, str(etiquette)))
    for a, b in zip(p.segments, p.segments[1:]):
        if b.debut < a.fin - 1e-6:
            erreur(f"segments qui se chevauchent ou dans le désordre : {fmt_temps(a.fin)} > {fmt_temps(b.debut)}")
    f = 0
    acc = 0.0
    for s in p.segments:
        acc += (s.fin - s.debut) / s.vitesse
        s.f0, s.f1 = f, max(f + 1, round(acc * p.fps))
        f = s.f1

    # cadrage
    transition = float(d.get("transition", 0.6))
    for n, c in enumerate(d.get("cadrage", []), 1):
        ou = f"cadrage n°{n}"
        p.cles.append(Cle(
            lire_temps(c.get("t", 0), f"{ou}.t"),
            lire_zone(c.get("zone", "tout"), media, ou),
            float(c.get("transition", transition)),
        ))
    p.cles.sort(key=lambda c: c.t)
    if not p.cles:
        p.cles.append(Cle(0.0, lire_zone("tout", media, ""), 0.0))

    for n, fl in enumerate(d.get("flou", []), 1):
        ou = f"flou n°{n}"
        z = lire_zone(fl.get("zone"), media, ou)
        p.flous.append(Flou(
            lire_temps(fl.get("debut", 0), f"{ou}.debut"),
            lire_temps(fl.get("fin", media.duree), f"{ou}.fin"),
            tuple(int(round(v)) for v in z),  # type: ignore[arg-type]
        ))

    for n, t in enumerate(d.get("texte", []), 1):
        ou = f"texte n°{n}"
        pos = t.get("position", "haut")
        if pos not in ("haut", "milieu", "bas"):
            erreur(f"{ou} : position « haut », « milieu » ou « bas »")
        p.textes.append(Texte(
            lire_temps(t.get("debut", 0), f"{ou}.debut"),
            lire_temps(t.get("fin", media.duree), f"{ou}.fin"),
            str(t.get("texte", "")), pos,
        ))
    return p


# ─── mise en page ───────────────────────────────────────────────────────────────


@dataclass
class Page:
    """Géométrie du canevas, en pixels réels (l'aperçu est à l'échelle 0.5)."""
    echelle: float
    disposition: str

    @property
    def W(self) -> int:
        return int(W_REF * self.echelle)

    @property
    def H(self) -> int:
        return int(H_REF * self.echelle)

    def boite(self) -> tuple[int, int, int, int]:
        """Rectangle où s'affiche la capture : x, y, w, h."""
        e = self.echelle
        if self.disposition == "plein":
            return 0, 0, self.W, self.H
        # « cadre » : 4:5, sous le bandeau de titre, au-dessus de la zone masquée par l'interface
        # des applis (légende, bouton musique) en bas.
        return 0, int(360 * e), self.W, int(1350 * e)

    def ref(self, v: float) -> int:
        return int(round(v * self.echelle))


def ease(u: float) -> float:
    u = min(max(u, 0.0), 1.0)
    return 4 * u ** 3 if u < 0.5 else 1 - (-2 * u + 2) ** 3 / 2


class Camera:
    """Trajectoire du cadrage : chaque clé démarre un mouvement à son instant (temps short)."""

    def __init__(self, plan: Plan):
        self.plan = plan
        self.cles = [(plan.vers_short(c.t), self._centre(c.zone), c.transition) for c in plan.cles]
        # position réelle au départ de chaque clé (continuité si une clé coupe la précédente)
        self.depart = [self.cles[0][1]]
        for k in range(1, len(self.cles)):
            t_prec, z_prec, tr_prec = self.cles[k - 1]
            u = 1.0 if tr_prec <= 0 else (self.cles[k][0] - t_prec) / tr_prec
            self.depart.append(self._mix(self.depart[k - 1], z_prec, ease(u)))

    @staticmethod
    def _centre(z):
        x, y, w, h = z
        return (x + w / 2, y + h / 2, w, h)

    @staticmethod
    def _mix(a, b, u):
        # centre linéaire, taille géométrique : un zoom paraît régulier à l'œil
        return (
            a[0] + (b[0] - a[0]) * u,
            a[1] + (b[1] - a[1]) * u,
            math.exp(math.log(a[2]) + (math.log(b[2]) - math.log(a[2])) * u),
            math.exp(math.log(a[3]) + (math.log(b[3]) - math.log(a[3])) * u),
        )

    def zone(self, t: float):
        k = 0
        for i, (tk, _, _) in enumerate(self.cles):
            if tk <= t + 1e-9:
                k = i
        tk, zk, tr = self.cles[k]
        if t < tk or tr <= 0:
            return zk if t >= tk else self.depart[0]
        return self._mix(self.depart[k], zk, ease((t - tk) / tr))


def cadrer(z, aspect: float, sw: int, sh: int):
    """Élargit la zone à l'aspect de la boîte sans sortir de la capture. Renvoie x, y, w, h."""
    cx, cy, w, h = z
    if w / h < aspect:
        w = h * aspect
    else:
        h = w / aspect
    w, h = min(w, sw), min(h, sh)
    cx = min(max(cx, w / 2), sw - w / 2)
    cy = min(max(cy, h / 2), sh - h / 2)
    return cx - w / 2, cy - h / 2, w, h


class Compositeur:
    def __init__(self, plan: Plan, page: Page):
        self.plan, self.page = plan, page
        self.camera = Camera(plan)
        self.bx, self.by, self.bw, self.bh = page.boite()
        self.rayon = 0 if plan.disposition == "plein" else page.ref(28)
        self._masques: dict[tuple[int, int], np.ndarray] = {}
        self.accent = couleur_bgr(plan.accent)

    def _masque(self, w: int, h: int) -> np.ndarray:
        cle = (w, h)
        if cle not in self._masques:
            m = np.zeros((h, w), np.uint8)
            r = min(self.rayon, w // 2, h // 2)
            cv2.rectangle(m, (r, 0), (w - r - 1, h - 1), 255, -1)
            cv2.rectangle(m, (0, r), (w - 1, h - r - 1), 255, -1)
            for cx, cy in ((r, r), (w - r - 1, r), (r, h - r - 1), (w - r - 1, h - r - 1)):
                cv2.circle(m, (cx, cy), r, 255, -1, cv2.LINE_AA)
            self._masques[cle] = (m.astype(np.float32) / 255.0)[..., None]
        return self._masques[cle]

    def _ombre(self, ox: int, oy: int, ow: int, oh: int) -> np.ndarray:
        """Facteur d'assombrissement de l'ombre portée ; recalculé seulement quand la carte change."""
        cle = (ox, oy, ow, oh)
        if getattr(self, "_ombre_cle", None) != cle:
            W, H = self.page.W, self.page.H
            o = np.zeros((H, W), np.float32)
            dec = self.page.ref(10)
            cv2.rectangle(o, (ox, oy + dec), (ox + ow, oy + oh + dec), 1.0, -1)
            o = cv2.GaussianBlur(o, (0, 0), self.page.ref(22))[..., None]
            self._ombre_cle, self._ombre_val = cle, 1 - 0.55 * o
        return self._ombre_val

    def flouter(self, img: np.ndarray, t_capture: float) -> None:
        for f in self.plan.flous:
            if f.debut <= t_capture <= f.fin:
                x, y, w, h = f.zone
                roi = img[y:y + h, x:x + w]
                if roi.size == 0:
                    continue
                petit = cv2.resize(roi, (max(1, w // 24), max(1, h // 24)), interpolation=cv2.INTER_AREA)
                img[y:y + h, x:x + w] = cv2.resize(petit, (w, h), interpolation=cv2.INTER_NEAREST)

    def composer(self, img: np.ndarray, i: int) -> np.ndarray:
        W, H = self.page.W, self.page.H
        sh, sw = img.shape[:2]
        self.flouter(img, self.plan.vers_capture(i))
        t = i / self.plan.fps
        x, y, w, h = cadrer(self.camera.zone(t), self.bw / self.bh, sw, sh)

        # fond : la capture floutée et assombrie, en « cover », centrée sur la caméra
        fa = W / H
        fw, fh = (sh * fa, sh) if sw / sh > fa else (sw, sw / fa)
        fx = min(max(x + w / 2 - fw / 2, 0), sw - fw)
        fy = min(max(y + h / 2 - fh / 2, 0), sh - fh)
        fond = img[int(fy):int(fy + fh), int(fx):int(fx + fw)]
        fond = cv2.resize(fond, (max(1, W // 12), max(1, H // 12)), interpolation=cv2.INTER_AREA)
        fond = cv2.GaussianBlur(fond, (0, 0), 3)
        canevas = cv2.resize(fond, (W, H), interpolation=cv2.INTER_LINEAR)
        canevas = cv2.convertScaleAbs(canevas, alpha=0.38, beta=0)

        # capture cadrée, ajustée dans la boîte
        s = min(self.bw / w, self.bh / h)
        ow, oh = max(2, int(round(w * s))), max(2, int(round(h * s)))
        ox, oy = self.bx + (self.bw - ow) // 2, self.by + (self.bh - oh) // 2
        if s < 1:
            # réduction : moyenne de zone, la seule qui garde le texte d'une interface lisible
            x0, y0 = int(round(x)), int(round(y))
            roi = img[y0:y0 + int(round(h)), x0:x0 + int(round(w))]
            vue = cv2.resize(roi, (ow, oh), interpolation=cv2.INTER_AREA)
        else:
            # agrandissement : sous-pixel, sinon les panoramiques lents tremblent
            m = np.float32([[s, 0, -x * s], [0, s, -y * s]])
            vue = cv2.warpAffine(img, m, (ow, oh), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE)

        zone = canevas[oy:oy + oh, ox:ox + ow]
        if self.rayon:
            # ombre portée douce sous la carte
            canevas = (canevas * self._ombre(ox, oy, ow, oh)).astype(np.uint8)
            zone = canevas[oy:oy + oh, ox:ox + ow]
            a = self._masque(ow, oh)
            zone[:] = (vue * a + zone * (1 - a)).astype(np.uint8)
        else:
            zone[:] = vue

        if self.plan.barre_progression:
            ep = max(2, self.page.ref(8))
            yb = self.by + self.bh - ep if self.plan.disposition == "cadre" else H - ep
            fin = int(W * (i + 1) / self.plan.nb_images)
            cv2.rectangle(canevas, (0, yb), (W, yb + ep), (40, 40, 40), -1)
            cv2.rectangle(canevas, (0, yb), (fin, yb + ep), self.accent, -1)
        return canevas


# ─── lecture des images de la capture ───────────────────────────────────────────


def images_segment(plan: Plan, seg: Segment):
    """Produit exactement f1 - f0 images BGR du segment, à la cadence du short."""
    sw, sh = plan.media.largeur, plan.media.hauteur
    n = seg.f1 - seg.f0
    cmd = [
        "ffmpeg", "-v", "error", "-ss", f"{seg.debut:.4f}", "-i", str(plan.source),
        "-t", f"{seg.fin - seg.debut:.4f}", "-an",
        "-vf", f"setpts=(PTS-STARTPTS)/{seg.vitesse},fps={plan.fps}",
        "-f", "rawvideo", "-pix_fmt", "bgr24", "-",
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE)
    taille = sw * sh * 3
    derniere = None
    try:
        for _ in range(n):
            brut = proc.stdout.read(taille)
            if len(brut) == taille:
                derniere = np.frombuffer(brut, np.uint8).reshape(sh, sw, 3)
            elif derniere is None:
                erreur(f"aucune image lisible à {fmt_temps(seg.debut)} dans la capture")
            yield derniere.copy()  # la dernière image est répétée si le décodeur finit un poil tôt
    finally:
        proc.stdout.close()
        proc.kill()
        proc.wait()


def image_a(plan: Plan, t: float) -> np.ndarray:
    sw, sh = plan.media.largeur, plan.media.hauteur
    p = subprocess.run(
        ["ffmpeg", "-v", "error", "-ss", f"{t:.4f}", "-i", str(plan.source), "-frames:v", "1",
         "-f", "rawvideo", "-pix_fmt", "bgr24", "-"],
        capture_output=True,
    )
    if len(p.stdout) < sw * sh * 3:
        erreur(f"pas d'image à {fmt_temps(t)}")
    return np.frombuffer(p.stdout[: sw * sh * 3], np.uint8).reshape(sh, sw, 3).copy()


# ─── son ─────────────────────────────────────────────────────────────────────────


def construire_voix(plan: Plan, dest: Path) -> None:
    """Piste son du short, alignée image par image sur les segments."""
    parties, entrees = [], ["-i", str(plan.source)]
    for k, s in enumerate(plan.segments):
        dur = (s.f1 - s.f0) / plan.fps
        fmt = "aformat=sample_rates=48000:channel_layouts=stereo"
        if s.son and plan.media.a_du_son:
            tempo = ""
            v = s.vitesse
            while v > 2.0:  # atempo se chaîne pour les grands facteurs
                tempo += "atempo=2.0,"
                v /= 2.0
            while v < 0.5:
                tempo += "atempo=0.5,"
                v /= 0.5
            if abs(v - 1) > 1e-6:
                tempo += f"atempo={v:.6f},"
            parties.append(
                f"[0:a]atrim={s.debut:.4f}:{s.fin:.4f},asetpts=PTS-STARTPTS,{tempo}{fmt},"
                f"apad,atrim=0:{dur:.4f}[a{k}]"
            )
        else:
            parties.append(f"anullsrc=r=48000:cl=stereo,atrim=0:{dur:.4f},{fmt}[a{k}]")
    liens = "".join(f"[a{k}]" for k in range(len(plan.segments)))
    graphe = ";".join(parties) + f";{liens}concat=n={len(plan.segments)}:v=0:a=1[v]"
    executer(["ffmpeg", "-y", "-v", "error", *entrees, "-filter_complex", graphe, "-map", "[v]",
              "-c:a", "pcm_s16le", str(dest)], "le montage du son")


def mixer(plan: Plan, voix: Path, dest: Path, rapide: bool) -> None:
    """Musique sous la voix (compression latérale), fondu final, puis loudness -14 LUFS."""
    dur = plan.duree
    entrees = ["-i", str(voix)]
    if plan.musique:
        entrees += ["-stream_loop", "-1", "-i", plan.musique]
        fondu = max(0.0, dur - 1.5)
        pre = (
            f"[0:a]asplit=2[v][sc];"
            f"[1:a]aformat=sample_rates=48000:channel_layouts=stereo,volume={plan.musique_db}dB,"
            f"atrim=0:{dur:.4f},afade=t=in:d=0.8,afade=t=out:st={fondu:.3f}:d=1.5[m];"
            f"[m][sc]sidechaincompress=threshold=0.02:ratio=10:attack=15:release=450[md];"
            f"[v][md]amix=inputs=2:normalize=0:duration=first[mix]"
        )
    else:
        pre = "[0:a]anull[mix]"

    cible = f"I={plan.lufs}:TP=-1.0:LRA=11"
    if rapide:
        graphe = pre + f";[mix]loudnorm={cible}[out]"
    else:
        # deux passes : la première mesure, la seconde corrige linéairement (pas de pompage)
        mesure = executer(
            ["ffmpeg", "-hide_banner", "-nostats", *entrees, "-filter_complex",
             pre + f";[mix]loudnorm={cible}:print_format=json[out]", "-map", "[out]", "-f", "null", "-"],
            "la mesure de loudness",
        )
        bloc = mesure[mesure.rfind("{"): mesure.rfind("}") + 1]
        try:
            m = json.loads(bloc)
        except json.JSONDecodeError:
            m = None
        if m and m.get("input_i") not in (None, "-inf") and float(m["input_i"]) > -70:
            cible += (f":measured_I={m['input_i']}:measured_TP={m['input_tp']}:measured_LRA={m['input_lra']}"
                      f":measured_thresh={m['input_thresh']}:offset={m['target_offset']}:linear=true")
            graphe = pre + f";[mix]loudnorm={cible}[out]"
        else:
            graphe = pre + ";[mix]anull[out]"  # short muet : rien à normaliser
    executer(["ffmpeg", "-y", "-v", "error", *entrees, "-filter_complex", graphe, "-map", "[out]",
              "-ar", "48000", "-c:a", "pcm_s16le", str(dest)], "le mixage")


# ─── transcription ──────────────────────────────────────────────────────────────


def chemin_mots(plan: Plan) -> Path:
    return plan.chemin.with_suffix(".mots.json")


def transcrire(plan: Plan, voix: Path, forcer: bool) -> list[dict]:
    fichier = chemin_mots(plan)
    empreinte = plan.empreinte_montage()
    if fichier.exists() and not forcer:
        d = json.loads(fichier.read_text(encoding="utf-8"))
        if d.get("montage") == empreinte:
            info(f"sous-titres repris de {fichier.name} (tes corrections sont conservées)")
            return d["mots"]
        info(f"{fichier.name} correspond à un autre montage : nouvelle transcription")
    if not any(s.son for s in plan.segments) or not plan.media.a_du_son:
        return []
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        erreur("faster-whisper absent : pip install faster-whisper (ou sous_titres = false dans le plan)")
    info(f"transcription Whisper « {plan.modele_whisper} » ({plan.langue})…")
    try:
        modele = WhisperModel(plan.modele_whisper, device="auto", compute_type="auto")
    except Exception as e:  # téléchargement impossible, modèle inconnu, CUDA cassé…
        erreur(f"Whisper « {plan.modele_whisper} » ne se charge pas ({type(e).__name__}: {e}).\n"
               "  Le premier usage télécharge le modèle depuis huggingface.co ; "
               "sinon rends avec --sans-sous-titres.")
    morceaux, _ = modele.transcribe(
        str(voix), language=plan.langue, word_timestamps=True, vad_filter=True,
        initial_prompt=plan.vocabulaire or None, condition_on_previous_text=False,
    )
    mots = []
    for m in morceaux:
        for w in m.words or []:
            texte = w.word.strip()
            if texte:
                mots.append({"mot": texte, "debut": round(w.start, 3), "fin": round(w.end, 3)})
    fichier.write_text(
        json.dumps({"montage": empreinte, "aide": "Corrige « mot » librement ; garde debut/fin.",
                    "mots": mots}, ensure_ascii=False, indent=1),
        encoding="utf-8",
    )
    info(f"{len(mots)} mots → {fichier.name} (corrigeable à la main, repris au prochain rendu)")
    return mots


def grouper(mots: list[dict], max_mots: int, max_car: int = 18) -> list[list[dict]]:
    groupes, cour = [], []
    for m in mots:
        if cour:
            trop = len(cour) >= max_mots or sum(len(x["mot"]) + 1 for x in cour) + len(m["mot"]) > max_car
            pause = m["debut"] - cour[-1]["fin"] > 0.45
            phrase = cour[-1]["mot"][-1:] in ".?!…,;:"
            if trop or pause or phrase:
                groupes.append(cour)
                cour = []
        cour.append(m)
    if cour:
        groupes.append(cour)
    return groupes


# ─── sous-titres ASS ────────────────────────────────────────────────────────────


def t_ass(s: float) -> str:
    cs = int(round(max(s, 0) * 100))
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    sec, cs = divmod(cs, 100)
    return f"{h}:{m:02d}:{sec:02d}.{cs:02d}"


def echapper(t: str) -> str:
    return t.replace("\\", "\\\\").replace("{", "(").replace("}", ")").replace("\n", "\\N")


def ecrire_ass(plan: Plan, mots: list[dict], dest: Path) -> None:
    police, acc = plan.police, couleur_ass(plan.accent)
    blanc, noir = "&H00FFFFFF", "&H00000000"
    cadre = plan.disposition == "cadre"
    boite_y, boite_h = (360, 1350) if cadre else (0, 1920)
    sous_titre_bas = 1920 - (boite_y + boite_h) + 200 if cadre else 560  # au-dessus de l'UI des applis
    styles = [
        # Nom, police, taille, primaire, secondaire, contour, fond, gras, …, bord, contour, ombre, align, marges
        f"Style: Titre,{police},66,{noir},{noir},{acc},&H00000000,-1,0,0,0,100,100,0,0,3,16,0,8,90,90,{150 if cadre else 210},1",
        f"Style: Sous,{police},82,{blanc},{blanc},{noir},&H80000000,-1,0,0,0,100,100,0,0,1,7,2,2,90,90,{sous_titre_bas},1",
        f"Style: Note,{police},46,{blanc},{blanc},&H50101010,&H00000000,-1,0,0,0,100,100,0,0,3,14,0,8,90,90,0,1",
        f"Style: Vitesse,{police},40,{noir},{noir},{acc},&H00000000,-1,0,0,0,100,100,0,0,3,10,0,9,0,0,0,1",
    ]
    ev = []

    def dialogue(debut, fin, style, texte):
        if fin - debut >= 0.02:
            ev.append(f"Dialogue: 0,{t_ass(debut)},{t_ass(fin)},{style},,0,0,0,,{texte}")

    if plan.titre:
        fin = plan.titre_duree or plan.duree
        dialogue(0, fin, "Titre", "{\\fad(250,250)}" + echapper(plan.titre))

    for s in plan.segments:
        if s.etiquette:
            dialogue(s.f0 / plan.fps, s.f1 / plan.fps, "Vitesse",
                     f"{{\\pos(1040,{boite_y + 40 if cadre else 260})}}" + echapper(s.etiquette))

    y_note = {"haut": boite_y + 60 if cadre else 470, "milieu": boite_y + boite_h // 2 - 40,
              "bas": 1920 - sous_titre_bas - 260}
    for t in plan.textes:
        a, b = plan.vers_short(t.debut), plan.vers_short(t.fin)
        dialogue(a, b, "Note", f"{{\\pos(540,{y_note[t.position]})\\fad(200,200)}}" + echapper(t.texte))

    if plan.sous_titres and mots:
        groupes = grouper(mots, plan.mots_par_groupe)
        for g, groupe in enumerate(groupes):
            suivant = groupes[g + 1][0]["debut"] if g + 1 < len(groupes) else plan.duree
            fin_groupe = min(max(groupe[-1]["fin"], groupe[0]["debut"] + 0.3), suivant, plan.duree)
            if suivant - fin_groupe < 0.25:  # évite le clignotement entre deux groupes
                fin_groupe = suivant
            for k, m in enumerate(groupe):
                debut = groupe[0]["debut"] if k == 0 else m["debut"]
                fin = groupe[k + 1]["debut"] if k + 1 < len(groupe) else fin_groupe
                morceaux = []
                for j, x in enumerate(groupe):
                    txt = echapper(x["mot"].upper() if plan.majuscules else x["mot"])
                    if j == k:
                        txt = f"{{\\c{acc}\\fscx108\\fscy108}}{txt}{{\\r}}"
                    morceaux.append(txt)
                dialogue(debut, fin, "Sous", " ".join(morceaux))

    dest.write_text(
        "[Script Info]\nScriptType: v4.00+\nPlayResX: 1080\nPlayResY: 1920\nWrapStyle: 0\n"
        "ScaledBorderAndShadow: yes\nYCbCr Matrix: TV.709\n\n"
        "[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
        "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, "
        "Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        + "\n".join(styles)
        + "\n\n[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
        + "\n".join(ev) + "\n",
        encoding="utf-8",
    )


def filtre_ass(ass: Path, plan: Plan) -> str:
    def esc(p: str) -> str:
        return p.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")
    f = f"ass=filename={esc(str(ass))}"
    if plan.dossier_polices:
        f += f":fontsdir={esc(plan.dossier_polices)}"
    return f


# ─── commandes ──────────────────────────────────────────────────────────────────


def cmd_rendre(args) -> None:
    verifier_outils()
    plan = charger_plan(Path(args.plan).resolve())
    page = Page(0.5 if args.apercu else 1.0, plan.disposition)
    tmp = Path(tempfile.mkdtemp(prefix="shorts-"))
    try:
        voix, mix, ass = tmp / "voix.wav", tmp / "mix.wav", tmp / "sous-titres.ass"
        info(f"{plan.source.name} ({plan.media.largeur}×{plan.media.hauteur}, {fmt_temps(plan.media.duree)}) "
             f"→ short de {plan.duree:.1f} s, {len(plan.segments)} segment(s), disposition « {plan.disposition} »")

        mots = []
        if args.image is None:
            construire_voix(plan, voix)
            if plan.sous_titres and not args.sans_sous_titres:
                mots = transcrire(plan, voix, args.retranscrire)
        elif chemin_mots(plan).exists():
            d = json.loads(chemin_mots(plan).read_text(encoding="utf-8"))
            mots = d["mots"] if d.get("montage") == plan.empreinte_montage() else []
        ecrire_ass(plan, mots, ass)

        comp = Compositeur(plan, page)
        plan.sortie.parent.mkdir(parents=True, exist_ok=True)

        if args.image is not None:
            t_cap = lire_temps(args.image, "--image")
            t_short = plan.vers_short(t_cap)
            i = min(int(t_short * plan.fps), plan.nb_images - 1)
            img = comp.composer(image_a(plan, plan.vers_capture(i)), i)
            brut = tmp / "brut.png"
            cv2.imwrite(str(brut), img)
            png = plan.sortie.with_name(f"{plan.sortie.stem}-{fmt_temps(t_cap).replace(':', 'm')}.png")
            executer(["ffmpeg", "-y", "-v", "error", "-i", str(brut), "-vf",
                      f"setpts=PTS+{t_short:.3f}/TB,{filtre_ass(ass, plan)}", "-frames:v", "1", str(png)],
                     "l'image d'aperçu")
            info(f"capture {fmt_temps(t_cap)} = short {fmt_temps(t_short)} → {png}")
            return

        mixer(plan, voix, mix, rapide=args.apercu)

        encodeur = [
            "ffmpeg", "-y", "-v", "error", "-stats",
            "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{page.W}x{page.H}", "-r", str(plan.fps), "-i", "-",
            "-i", str(mix),
            "-vf", filtre_ass(ass, plan) + ",format=yuv420p",
            "-map", "0:v", "-map", "1:a",
            "-c:v", "libx264", "-preset", "ultrafast" if args.apercu else "slow",
            "-crf", "23" if args.apercu else "18", "-profile:v", "high", "-g", str(plan.fps * 2),
            "-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709",
            "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
            "-movflags", "+faststart", "-shortest", str(plan.sortie),
        ]
        info("rendu des images…")
        enc = subprocess.Popen(encodeur, stdin=subprocess.PIPE)
        i = 0
        try:
            for seg in plan.segments:
                for img in images_segment(plan, seg):
                    enc.stdin.write(comp.composer(img, i).tobytes())
                    i += 1
            enc.stdin.close()
        except BrokenPipeError:
            pass
        if enc.wait() != 0:
            erreur("l'encodage final a échoué (message ffmpeg ci-dessus)")
        final = sonder(plan.sortie)
        info(f"✓ {plan.sortie} — {final.largeur}×{final.hauteur}, {final.duree:.2f} s")
        if final.duree > 90:
            info("⚠ plus de 90 s : trop long pour un Reel Instagram standard")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def cmd_transcrire(args) -> None:
    verifier_outils()
    plan = charger_plan(Path(args.plan).resolve())
    tmp = Path(tempfile.mkdtemp(prefix="shorts-"))
    try:
        voix = tmp / "voix.wav"
        construire_voix(plan, voix)
        mots = transcrire(plan, voix, forcer=args.retranscrire)
        for g in grouper(mots, plan.mots_par_groupe):
            print(f"  {fmt_temps(g[0]['debut'])}  {' '.join(m['mot'] for m in g)}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def intervalles(stderr: str, debut: str, fin: str, duree: float) -> list[tuple[float, float]]:
    res, cour = [], None
    for ligne in stderr.splitlines():
        if (m := re.search(rf"{debut}: ?(-?[\d.]+)", ligne)):
            cour = max(0.0, float(m.group(1)))
        elif (m := re.search(rf"{fin}: ?([\d.]+)", ligne)) and cour is not None:
            res.append((cour, float(m.group(1))))
            cour = None
    if cour is not None:
        res.append((cour, duree))
    return res


def inter(a: list, b: list) -> list[tuple[float, float]]:
    res = []
    for x0, x1 in a:
        for y0, y1 in b:
            lo, hi = max(x0, y0), min(x1, y1)
            if hi > lo:
                res.append((lo, hi))
    return sorted(res)


def cmd_analyser(args) -> None:
    verifier_outils()
    src = Path(args.capture).resolve()
    if not src.exists():
        erreur(f"capture introuvable : {src}")
    media = sonder(src)
    info(f"analyse de {src.name} ({media.largeur}×{media.hauteur}, {fmt_temps(media.duree)})…")
    cmd = ["ffmpeg", "-hide_banner", "-nostats", "-i", str(src),
           "-vf", f"scale=480:-2,freezedetect=n={args.bruit_image}:d={args.fige}", "-map", "0:v"]
    if media.a_du_son:
        cmd += ["-af", f"silencedetect=n={args.silence_db}dB:d={args.fige}", "-map", "0:a"]
    cmd += ["-f", "null", "-"]
    err = subprocess.run(cmd, capture_output=True, text=True).stderr
    figes = intervalles(err, "freeze_start", "freeze_end", media.duree)
    silences = intervalles(err, "silence_start", "silence_end", media.duree) if media.a_du_son \
        else [(0.0, media.duree)]

    marge = 0.35
    # figé ET silencieux → coupé ; silencieux mais ça bouge, longtemps → accéléré
    morts = [(a + marge, b - marge) for a, b in inter(figes, silences) if b - a - 2 * marge >= 1.0]
    bouge = [(a + marge, b - marge) for a, b in silences if b - a >= args.accelerer_apres]
    # retire des accélérations ce qui est déjà coupé
    evenements = [(a, b, "coupe") for a, b in morts]
    for a, b in bouge:
        cur = a
        for m0, m1 in sorted(morts):
            if m1 <= cur or m0 >= b:
                continue
            if m0 - cur >= args.accelerer_apres:
                evenements.append((cur, m0, "vite"))
            cur = max(cur, m1)
        if b - cur >= args.accelerer_apres:
            evenements.append((cur, b, "vite"))
    evenements.sort()

    segments, cur = [], 0.0
    for a, b, quoi in evenements:
        if a > cur + 0.05:
            segments.append((cur, a, 1.0))
        if quoi == "vite":
            segments.append((a, b, args.facteur))
        cur = max(cur, b)
    if media.duree - cur > 0.05:
        segments.append((cur, media.duree, 1.0))

    apres = sum((b - a) / v for a, b, v in segments)
    sortie = Path(args.o) if args.o else Path("plans") / f"{src.stem}.toml"
    sortie.parent.mkdir(parents=True, exist_ok=True)
    rel = Path(__import__("os").path.relpath(src, sortie.parent.resolve()))
    lignes = [
        f"# Plan proposé par « shorts.py analyser » — {fmt_temps(media.duree)} → {fmt_temps(apres)}",
        "# Tous les temps sont ceux de la CAPTURE. Relis, ajuste, puis : python shorts.py rendre <ce fichier>",
        "",
        f'source = "{rel.as_posix()}"',
        f'# sortie = "../sorties/{sortie.stem}.mp4"',
        'titre = "Kora fait … \\nsans que je touche au clavier"   # accroche : 2 lignes max',
        'disposition = "cadre"            # « cadre » (carte 4:5 sur fond flouté) ou « plein » (9:16)',
        'vocabulaire = "Kora, Choragos, Ollama, Florent."   # aide Whisper à écrire les noms propres',
        '# musique = "../musiques/fond.mp3"   # baissée automatiquement quand quelqu\'un parle',
        "",
        "# Cadrage : à l'instant t, la caméra part vers la zone [x, y, largeur, hauteur] (pixels de la capture).",
        "# Règle-le image par image : python shorts.py rendre <plan> --image 00:12",
        "[[cadrage]]",
        't = "00:00"',
        'zone = "tout"',
        "",
        "# [[cadrage]]",
        '# t = "00:08.5"',
        f"# zone = [{media.largeur // 2}, 0, {media.largeur // 2}, {media.hauteur}]   # moitié droite, par exemple",
        "",
        "# Masque ce qui ne doit pas sortir de ton écran (courriels, chemins, noms) :",
        "# [[flou]]",
        '# debut = "00:00"',
        f'# fin = "{fmt_temps(media.duree)}"',
        "# zone = [0, 0, 400, 60]",
        "",
    ]
    for a, b, v in segments:
        lignes += ["[[segment]]", f'debut = "{fmt_temps(a)}"', f'fin = "{fmt_temps(b)}"']
        if v != 1:
            lignes.append(f"vitesse = {v:g}   # silencieux mais l'écran bouge : Kora travaille")
        lignes.append("")
    sortie.write_text("\n".join(lignes), encoding="utf-8")
    coupes = sum(1 for e in evenements if e[2] == "coupe")
    vites = sum(1 for e in evenements if e[2] == "vite")
    info(f"{coupes} temps mort(s) coupé(s), {vites} passage(s) accéléré(s) ×{args.facteur:g} : "
         f"{fmt_temps(media.duree)} → {fmt_temps(apres)}")
    info(f"plan écrit : {sortie}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = ap.add_subparsers(dest="commande", required=True)

    a = sp.add_parser("analyser", help="propose un plan qui coupe les temps morts d'une capture")
    a.add_argument("capture")
    a.add_argument("-o", help="plan TOML à écrire (défaut : plans/<capture>.toml)")
    a.add_argument("--fige", type=float, default=1.5, help="durée min d'un écran figé/silence (s)")
    a.add_argument("--silence-db", type=float, default=-38, help="seuil de silence (dB)")
    a.add_argument("--bruit-image", type=float, default=0.003, help="tolérance de freezedetect")
    a.add_argument("--accelerer-apres", type=float, default=4.0, help="silence animé à accélérer au-delà de (s)")
    a.add_argument("--facteur", type=float, default=3.0, help="facteur d'accélération proposé")
    a.set_defaults(f=cmd_analyser)

    r = sp.add_parser("rendre", help="rend le short décrit par un plan")
    r.add_argument("plan")
    r.add_argument("--apercu", action="store_true", help="540×960, encodage rapide")
    r.add_argument("--image", help="rend seulement l'image à ce temps de capture (PNG)")
    r.add_argument("--retranscrire", action="store_true", help="ignore la transcription existante")
    r.add_argument("--sans-sous-titres", action="store_true")
    r.set_defaults(f=cmd_rendre)

    t = sp.add_parser("transcrire", help="transcrit seulement, pour corriger <plan>.mots.json")
    t.add_argument("plan")
    t.add_argument("--retranscrire", action="store_true")
    t.set_defaults(f=cmd_transcrire)

    args = ap.parse_args()
    args.f(args)


if __name__ == "__main__":
    main()
