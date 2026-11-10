"""
Vidéo explicative d'une fonctionnalité : une diapositive par scène du document (Pillow), lue par une voix de
synthèse française, assemblée en MP4 avec des sous-titres (ffmpeg fourni par imageio-ffmpeg).

Voix, sans envoyer le texte à un service externe :
- Windows : voix SAPI installées (System.Speech, par PowerShell), la première voix française trouvée ;
- Linux : espeak-ng, s'il est installé ;
- sinon, ou avec DOCUMENTATION_VOIX=aucune : vidéo muette, la narration est incrustée en sous-titres.
DOCUMENTATION_VOIX peut aussi donner une partie du nom de la voix voulue (ex. « Hortense »).
"""
import os
import platform
import shutil
import subprocess
import tempfile
import wave
from pathlib import Path

from . import config

LARGEUR, HAUTEUR = 1920, 1080
FOND, ACCENT, ACCENT_CLAIR = (247, 248, 250), (31, 78, 121), (222, 233, 244)
TEXTE, GRIS, BLANC = (29, 36, 48), (91, 101, 115), (255, 255, 255)
MARGE, BANDEAU = 110, 170
PAUSE = 0.7                       # secondes de silence après chaque narration
MOTS_PAR_SECONDE = 2.5            # rythme de lecture des sous-titres d'une vidéo muette
SANS_FENETRE = 0x08000000 if os.name == "nt" else 0

POLICES = {
    False: ["C:/Windows/Fonts/segoeui.ttf", "C:/Windows/Fonts/arial.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/Library/Fonts/Arial.ttf"],
    True: ["C:/Windows/Fonts/segoeuib.ttf", "C:/Windows/Fonts/arialbd.ttf",
           "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "/Library/Fonts/Arial Bold.ttf"],
}

SCRIPT_SAPI = r"""
param([string]$texte, [string]$sortie, [string]$voulue)
Add-Type -AssemblyName System.Speech
$s = New-Object System.Speech.Synthesis.SpeechSynthesizer
$voix = $s.GetInstalledVoices() | Where-Object { $_.Enabled } | ForEach-Object { $_.VoiceInfo }
if ($voulue) { $voix = $voix | Where-Object { $_.Name -like "*$voulue*" } }
else { $voix = $voix | Where-Object { $_.Culture.Name -like 'fr*' } }
$v = $voix | Select-Object -First 1
if (-not $v) { exit 3 }
$s.SelectVoice($v.Name)
$s.Rate = 1
if ($sortie) {
  $s.SetOutputToWaveFile($sortie)
  $s.Speak([IO.File]::ReadAllText($texte, [Text.Encoding]::UTF8))
}
$s.Dispose()
Write-Output $v.Name
"""


class VideoErreur(Exception):
    pass


# ---------------------------------------------------------------------------
# Séquences : ce qui est montré et lu
# ---------------------------------------------------------------------------

def sequences(document: dict) -> list:
    """[{type, titre, points, narration, capture}] : titre, scènes, cas de test, points d'attention, fin."""
    suite = [{"type": "titre", "titre": document["titre"], "points": [document["resume"]],
              "narration": f"{document['titre']}. {document['resume']}"}]
    for scene in document["scenes"]:
        suite.append({"type": "scene", "titre": scene["titre"], "points": list(scene.get("points") or []),
                      "narration": scene["narration"], "capture": scene.get("capture") or ""})
    for i, cas in enumerate(document.get("cas_de_test") or [], start=1):
        etapes = cas.get("etapes") or []
        points = [f"{e.get('ecran') + ' : ' if e.get('ecran') else ''}{e.get('action', '')} → {e.get('attendu', '')}"
                  for e in etapes[:6]]
        if len(etapes) > 6:
            points[-1] = f"… et {len(etapes) - 5} autres étapes, détaillées dans le document"
        lu = [f"Cas de test {i} : {cas['titre']}."]
        if cas.get("prerequis"):
            lu.append(f"Prérequis : {cas['prerequis'].rstrip('.')}.")
        for n, e in enumerate(etapes[:4], start=1):
            lu.append(f"{'Ensuite' if n > 1 else 'D’abord'}, {e.get('action', '').rstrip('.')} ; on doit constater "
                      f"que {e.get('attendu', '').rstrip('.')}.")
        if len(etapes) > 4:
            lu.append("Les étapes suivantes sont détaillées dans le document joint.")
        suite.append({"type": "test", "titre": f"Cas de test {i} — {cas['titre']}", "points": points,
                      "narration": " ".join(lu), "sous_titre": cas.get("prerequis", "")})
    if document.get("points_attention"):
        suite.append({"type": "scene", "titre": "Points d'attention", "points": document["points_attention"][:6],
                      "narration": "Points d'attention. " + " ".join(p.rstrip(".") + "." for p in
                                                                    document["points_attention"][:6])})
    suite.append({"type": "fin", "titre": "Pour aller plus loin",
                  "points": ["Le document joint détaille le paramétrage, chaque cas de test et les sources."],
                  "narration": "Le document joint détaille le paramétrage, chaque cas de test, et les sources de "
                               "cette présentation."})
    return suite


# ---------------------------------------------------------------------------
# Diapositives
# ---------------------------------------------------------------------------

def _police(taille: int, gras: bool = False):
    from PIL import ImageFont
    for chemin in POLICES[gras]:
        if Path(chemin).exists():
            return ImageFont.truetype(chemin, taille)
    return ImageFont.load_default(size=taille)


def _couper(texte: str, police, largeur: int) -> list:
    lignes, ligne = [], ""
    for mot in str(texte).split():
        essai = f"{ligne} {mot}".strip()
        if police.getlength(essai) <= largeur or not ligne:
            ligne = essai
        else:
            lignes.append(ligne)
            ligne = mot
    return lignes + ([ligne] if ligne else [])


def _paragraphes(dessin, points: list, x: int, y: int, largeur: int, hauteur_max: int, puces: bool = True) -> int:
    """Écrit des points à puces en réduisant la taille si besoin ; renvoie le y final."""
    for taille in (46, 42, 38, 34, 30, 27):
        police = _police(taille)
        interligne, ecart = int(taille * 1.32), int(taille * 0.75)
        blocs = [_couper(p, police, largeur - (60 if puces else 0)) for p in points]
        hauteur = sum(len(b) * interligne + ecart for b in blocs)
        if hauteur <= hauteur_max or taille == 27:
            break
    for bloc in blocs:
        if puces:
            r = taille // 5
            dessin.ellipse([x + 8, y + interligne // 2 - r, x + 8 + 2 * r, y + interligne // 2 + r], fill=ACCENT)
        for ligne in bloc:
            if y + interligne > HAUTEUR - 90:
                return y
            dessin.text((x + (60 if puces else 0), y), ligne, font=police, fill=TEXTE)
            y += interligne
        y += ecart
    return y


def _bande_sous_titre(image, texte: str) -> None:
    from PIL import ImageDraw
    police = _police(36)
    lignes = _couper(texte, police, LARGEUR - 2 * MARGE)[:3]
    hauteur = 40 + len(lignes) * 50
    calque = ImageDraw.Draw(image, "RGBA")
    calque.rectangle([0, HAUTEUR - hauteur - 100, LARGEUR, HAUTEUR - 100], fill=(15, 20, 30, 215))
    y = HAUTEUR - hauteur - 80
    for ligne in lignes:
        largeur = police.getlength(ligne)
        calque.text(((LARGEUR - largeur) / 2, y), ligne, font=police, fill=BLANC)
        y += 50


def diapositive(seq: dict, numero: int, total: int, sujet: str, dossier: Path, sous_titre: str = ""):
    from PIL import Image, ImageDraw, ImageOps

    if seq["type"] == "titre":
        image = Image.new("RGB", (LARGEUR, HAUTEUR), ACCENT)
        dessin = ImageDraw.Draw(image)
        dessin.text((MARGE, 220), sujet.upper(), font=_police(34, True), fill=ACCENT_CLAIR)
        y = 300
        for ligne in _couper(seq["titre"], _police(78, True), LARGEUR - 2 * MARGE)[:3]:
            dessin.text((MARGE, y), ligne, font=_police(78, True), fill=BLANC)
            y += 100
        dessin.rectangle([MARGE, y + 30, MARGE + 160, y + 38], fill=ACCENT_CLAIR)
        y += 90
        for ligne in _couper(seq["points"][0], _police(40), LARGEUR - 2 * MARGE)[:6]:
            dessin.text((MARGE, y), ligne, font=_police(40), fill=ACCENT_CLAIR)
            y += 56
        dessin.text((MARGE, HAUTEUR - 110), "Documentation fonctionnelle — Agent Odatio", font=_police(28),
                    fill=ACCENT_CLAIR)
    else:
        image = Image.new("RGB", (LARGEUR, HAUTEUR), FOND)
        dessin = ImageDraw.Draw(image)
        dessin.rectangle([0, 0, LARGEUR, BANDEAU], fill=ACCENT)
        titre = _couper(seq["titre"], _police(56, True), LARGEUR - 2 * MARGE)
        police_titre = _police(56 if len(titre) == 1 else 44, True)
        titre = _couper(seq["titre"], police_titre, LARGEUR - 2 * MARGE)[:2]
        y = (BANDEAU - len(titre) * int(police_titre.size * 1.2)) // 2
        for ligne in titre:
            dessin.text((MARGE, y), ligne, font=police_titre, fill=BLANC)
            y += int(police_titre.size * 1.2)

        haut, bas = BANDEAU + 70, HAUTEUR - (290 if sous_titre else 110)
        capture = Path(dossier) / "captures" / seq.get("capture", "") if seq.get("capture") else None
        if capture and capture.is_file():
            largeur_texte = int((LARGEUR - 2 * MARGE) * 0.42)
            _paragraphes(dessin, seq["points"], MARGE, haut, largeur_texte, bas - haut)
            zone = (LARGEUR - MARGE - (MARGE + largeur_texte + 60), bas - haut)
            try:
                img = ImageOps.contain(Image.open(capture).convert("RGB"), zone)
                x = LARGEUR - MARGE - img.width
                dessin.rectangle([x - 3, haut - 3, x + img.width + 2, haut + img.height + 2], outline=(200, 205, 212),
                                 width=3)
                image.paste(img, (x, haut))
            except OSError:
                pass
        elif seq["type"] == "fin":
            _paragraphes(dessin, seq["points"], MARGE, haut + 120, LARGEUR - 2 * MARGE, bas - haut, puces=False)
        else:
            _paragraphes(dessin, seq["points"], MARGE, haut, LARGEUR - 2 * MARGE, bas - haut)
            if seq["type"] == "test" and seq.get("sous_titre") and not sous_titre:
                dessin.text((MARGE, bas - 10), "Prérequis : " + seq["sous_titre"][:150], font=_police(28), fill=GRIS)

        dessin.text((MARGE, HAUTEUR - 70), sujet, font=_police(26), fill=GRIS)
        repere = f"{numero} / {total}"
        dessin.text((LARGEUR - MARGE - _police(26).getlength(repere), HAUTEUR - 70), repere, font=_police(26),
                    fill=GRIS)
    if sous_titre:
        _bande_sous_titre(image, sous_titre)
    return image


# ---------------------------------------------------------------------------
# Voix
# ---------------------------------------------------------------------------

_MOTEUR = None


def _executer(commande: list, delai: int = 300) -> subprocess.CompletedProcess:
    return subprocess.run(commande, capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=delai, creationflags=SANS_FENETRE)


def _script_sapi() -> Path:
    chemin = config.CACHE / "voix_sapi.ps1"
    chemin.parent.mkdir(parents=True, exist_ok=True)
    if not chemin.exists() or chemin.read_text(encoding="utf-8-sig") != SCRIPT_SAPI:
        chemin.write_text(SCRIPT_SAPI, encoding="utf-8-sig")  # BOM : PowerShell 5 lit alors l'UTF-8
    return chemin


def _commande_sapi(texte: str = "", sortie: str = "", voulue: str = "") -> list:
    """Commande PowerShell ; seuls les paramètres renseignés sont passés (un argument vide est mal reçu)."""
    commande = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(_script_sapi())]
    for nom, valeur in (("-texte", texte), ("-sortie", sortie), ("-voulue", voulue)):
        if valeur:
            commande += [nom, valeur]
    return commande


def moteur_voix() -> tuple:
    """(moteur, nom de la voix) : ("sapi", "Microsoft Hortense…"), ("espeak-ng", "fr") ou ("", "")."""
    global _MOTEUR
    if _MOTEUR is not None:
        return _MOTEUR
    voulue = config.secret("DOCUMENTATION_VOIX")
    _MOTEUR = ("", "")
    if voulue.lower() in ("aucune", "non", "0"):
        return _MOTEUR
    voulue = "" if voulue.lower() == "auto" else voulue
    if platform.system() == "Windows":
        try:
            r = _executer(_commande_sapi(voulue=voulue), delai=60)
            if r.returncode == 0 and r.stdout.strip():
                _MOTEUR = ("sapi", r.stdout.strip().splitlines()[-1])
        except (OSError, subprocess.SubprocessError):
            pass
    elif shutil.which("espeak-ng"):
        _MOTEUR = ("espeak-ng", voulue or "fr")
    return _MOTEUR


def synthetiser(texte: str, sortie: Path) -> bool:
    moteur, voix = moteur_voix()
    if not moteur:
        return False
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as f:
        f.write(texte)
        source = f.name
    try:
        if moteur == "sapi":
            voulue = config.secret("DOCUMENTATION_VOIX")
            r = _executer(_commande_sapi(source, str(sortie), "" if voulue.lower() in ("", "auto") else voulue))
        else:
            r = _executer(["espeak-ng", "-v", voix, "-s", "155", "-w", str(sortie), "-f", source])
        return r.returncode == 0 and sortie.exists() and sortie.stat().st_size > 1000
    except (OSError, subprocess.SubprocessError):
        return False
    finally:
        os.unlink(source)


def _duree_wav(chemin: Path) -> float:
    with wave.open(str(chemin), "rb") as w:
        return w.getnframes() / float(w.getframerate())


# ---------------------------------------------------------------------------
# Montage
# ---------------------------------------------------------------------------

def _ffmpeg() -> str:
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except (ImportError, RuntimeError):
        if shutil.which("ffmpeg"):
            return shutil.which("ffmpeg")
    raise VideoErreur("ffmpeg introuvable : installe imageio-ffmpeg (pip install imageio-ffmpeg).")


def _morceaux(texte: str, longueur: int = 110) -> list:
    """Découpe une narration en sous-titres courts, aux fins de phrase ou aux virgules."""
    import re
    phrases = re.split(r"(?<=[.!?;:])\s+", texte.strip())
    morceaux = []
    for phrase in phrases:
        while len(phrase) > longueur:
            coupe = phrase.rfind(", ", 0, longueur)
            coupe = coupe + 1 if coupe > 40 else (phrase.rfind(" ", 0, longueur) if phrase.rfind(" ", 0, longueur) > 0
                                                   else longueur)
            morceaux.append(phrase[:coupe].strip())
            phrase = phrase[coupe:].strip()
        if phrase:
            morceaux.append(phrase)
    return morceaux or [""]


def _horodatage(secondes: float) -> str:
    ms = int(round(secondes * 1000))
    return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"


def _segment(ffmpeg: str, image: Path, audio: Path | None, duree: float, sortie: Path) -> None:
    commande = [ffmpeg, "-y", "-loglevel", "error", "-loop", "1", "-framerate", "5", "-i", str(image)]
    commande += ["-i", str(audio)] if audio else ["-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono"]
    commande += ["-af", f"apad=pad_dur={PAUSE}" if audio else "anull", "-t", f"{duree:.2f}",
                 "-c:v", "libx264", "-tune", "stillimage", "-preset", "veryfast", "-pix_fmt", "yuv420p", "-r", "25",
                 "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "1", str(sortie)]
    r = _executer(commande)
    if r.returncode != 0:
        raise VideoErreur(f"ffmpeg a échoué sur {image.name} : {r.stderr.strip()[-400:]}")


def produire(document: dict, dossier: Path, sortie: Path, sujet: str = "", journal=print) -> dict:
    """Rend la vidéo : {"video", "sous_titres", "voix", "duree"}."""
    ffmpeg = _ffmpeg()
    sujet = sujet or document.get("titre", "")
    suite = sequences(document)
    moteur, voix = moteur_voix()
    journal(f"Voix : {voix}." if moteur else "Aucune voix française disponible : vidéo muette, narration en "
                                             "sous-titres.")
    travail = Path(dossier) / "_rendu"
    if travail.exists():
        shutil.rmtree(travail)
    travail.mkdir()
    segments, srt, t, n = [], [], 0.0, 1
    try:
        for i, seq in enumerate(suite, start=1):
            journal(f"Diapositive {i}/{len(suite)} : {seq['titre'][:60]}")
            morceaux = _morceaux(seq["narration"])
            audio = travail / f"{i:02d}.wav"
            if moteur and synthetiser(seq["narration"], audio):
                duree = _duree_wav(audio) + PAUSE
                image = travail / f"{i:02d}.png"
                diapositive(seq, i, len(suite), sujet, dossier).save(image)
                segment = travail / f"{i:02d}.mp4"
                _segment(ffmpeg, image, audio, duree, segment)
                segments.append(segment)
                total_car = sum(len(m) for m in morceaux) or 1
                debut = t
                for m in morceaux:
                    fin = debut + (duree - PAUSE) * len(m) / total_car
                    srt.append(f"{n}\n{_horodatage(debut)} --> {_horodatage(fin)}\n{m}\n")
                    n, debut = n + 1, fin
                t += duree
            else:  # muet : une image par sous-titre incrusté
                for j, m in enumerate(morceaux, start=1):
                    duree = max(2.5, len(m.split()) / MOTS_PAR_SECONDE)
                    image = travail / f"{i:02d}-{j:02d}.png"
                    diapositive(seq, i, len(suite), sujet, dossier, sous_titre=m).save(image)
                    segment = travail / f"{i:02d}-{j:02d}.mp4"
                    _segment(ffmpeg, image, None, duree, segment)
                    segments.append(segment)
                    srt.append(f"{n}\n{_horodatage(t)} --> {_horodatage(t + duree)}\n{m}\n")
                    n, t = n + 1, t + duree

        liste = travail / "liste.txt"
        liste.write_text("".join(f"file '{s.name}'\n" for s in segments), encoding="utf-8")
        sous_titres = sortie.with_suffix(".srt")
        sous_titres.write_text("\n".join(srt), encoding="utf-8")
        assemblee = travail / "assemblee.mp4"
        journal("Assemblage de la vidéo…")
        r = _executer([ffmpeg, "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(liste),
                       "-c", "copy", str(assemblee)])
        if r.returncode != 0:
            raise VideoErreur(f"Assemblage impossible : {r.stderr.strip()[-400:]}")
        r = _executer([ffmpeg, "-y", "-loglevel", "error", "-i", str(assemblee), "-i", str(sous_titres),
                       "-map", "0", "-map", "1", "-c", "copy", "-c:s", "mov_text", "-metadata:s:s:0", "language=fre",
                       "-movflags", "+faststart", str(sortie)])
        if r.returncode != 0:
            shutil.copyfile(assemblee, sortie)
    finally:
        shutil.rmtree(travail, ignore_errors=True)
    journal(f"Vidéo prête : {t / 60:.1f} min.")
    return {"video": sortie, "sous_titres": sous_titres, "voix": voix if moteur else "", "duree": t}
