#!/usr/bin/env python3
"""Labyrinthe à main levée : guide le point jusqu'à l'arrivée sans toucher les bords.

Tape le mot secret pour sortir.
"""
import atexit
import json
import math
import os
import random
import signal
import subprocess
import time
import tkinter as tk

MOT_SECRET = "laissemoisortir"
DOT_R = 7
SENS = 3.0
# Le curseur physique est ramené au centre dès qu'il approche d'un bord : sinon
# le système cesse d'émettre du mouvement dans cette direction et le point se
# fige alors que la souris bouge encore.
MARGE_RECENTRE = 220

RACCOURCIS = [
    ("org.gnome.desktop.wm.keybindings", k) for k in (
        "switch-applications", "switch-applications-backward",
        "switch-windows", "switch-windows-backward",
        "switch-group", "switch-group-backward",
        "switch-panels", "switch-panels-backward",
    )
]
SAUVEGARDE = os.path.expanduser("~/.cache/labyrinthe-raccourcis.json")
RING = [(math.cos(math.radians(a)), math.sin(math.radians(a))) for a in range(0, 360, 45)]

BG_HAUT, BG_BAS = "#05070f", "#0e1526"
CORRIDOR = "#070d16"
# Halo du tracé, du plus large au plus serré : peint dans cet ordre, chaque
# couche recouvre la précédente et le dégradé se forme sans coutures aux
# recoupements des segments.
HALO = [(13, "#06202c"), (9, "#0a3a50"), (6, "#116078"), (3, "#1b98bc"), (1, "#5ce8ff")]
NEON = "#5ce8ff"
DEPART, ARRIVEE = "#2bff88", "#ff46e0"
POINT, ENCRE, ROUGE = "#ffffff", "#8da4bd", "#ff2d55"

FONT = ("DejaVu Sans Mono", 13)
HUD = ("DejaVu Sans Mono", 12)
TITRE = ("DejaVu Sans Mono", 40, "bold")
SOUS = ("DejaVu Sans Mono", 15)

TRAIL_N = 9
# Secousse de mort : la somme des décalages vaut (0, 0), sinon la scène
# resterait décalée après l'animation.
SECOUSSE = [(7, 0), (-12, 3), (9, -4), (-5, 2), (1, -1)]


def _gsettings(*args):
    try:
        return subprocess.run(["gsettings", *args], capture_output=True,
                              text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def restaurer_raccourcis():
    """Remet les raccourcis tels qu'ils étaient. Idempotent."""
    try:
        with open(SAUVEGARDE) as f:
            sauv = json.load(f)
    except (OSError, ValueError):
        return
    for cle, val in sauv.items():
        schema, key = cle.split(" ", 1)
        _gsettings("set", schema, key, val)
    try:
        os.remove(SAUVEGARDE)
    except OSError:
        pass


def couper_raccourcis():
    # Un fichier déjà présent signale une session précédente tuée brutalement :
    # on répare avant de resauvegarder, sinon on archiverait des listes vides.
    restaurer_raccourcis()
    sauv = {}
    for schema, key in RACCOURCIS:
        sauv[f"{schema} {key}"] = _gsettings("get", schema, key) or "@as []"
    os.makedirs(os.path.dirname(SAUVEGARDE), exist_ok=True)
    with open(SAUVEGARDE, "w") as f:
        json.dump(sauv, f)
    for schema, key in RACCOURCIS:
        _gsettings("set", schema, key, "[]")


def melange(c1, c2, t):
    a = [int(c1[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(c2[i:i + 2], 16) for i in (1, 3, 5)]
    return "#%02x%02x%02x" % tuple(int(x + (y - x) * t) for x, y in zip(a, b))


def serpentine(lanes, width, connector_width=None):
    connector_width = connector_width or width
    steps, sign = [], 1
    for lane in range(lanes):
        steps.append(("H", sign * 1.0, width))
        sign *= -1
        if lane < lanes - 1:
            steps.append(("V", 1.0 / (lanes - 1), connector_width))
    return steps


NIVEAUX = [serpentine(4, 70), serpentine(6, 46, 34), serpentine(8, 30, 22)]
MARGES = [100, 140, 170]


class Jeu(tk.Tk):
    def __init__(self):
        super().__init__()
        self.configure(bg=BG_HAUT)
        self.attributes("-fullscreen", True)
        self.protocol("WM_DELETE_WINDOW", lambda: None)
        self.canvas = tk.Canvas(self, bg=BG_HAUT, highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self.token, self.secret_buf, self._demarre = 0, "", False
        self.morts, self.t0 = 0, None
        self.bind_all("<Key>", self.on_key)
        self.canvas.bind("<Configure>", self._premier_affichage, add="+")
        # os._exit court-circuite atexit : restaurer explicitement d'abord.
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            signal.signal(sig, lambda *_: (restaurer_raccourcis(), os._exit(0)))
        self._keepalive()

    def _keepalive(self):
        self.after(150, self._keepalive)

    def _premier_affichage(self, e):
        if self._demarre or self.canvas.winfo_width() < 10:
            return
        self._demarre = True
        self.t0 = time.time()
        self.carte_niveau(0)

    def on_key(self, e):
        if e.char:
            self.secret_buf = (self.secret_buf + e.char)[-(len(MOT_SECRET) + 5):]
            if self.secret_buf.lower().endswith(MOT_SECRET.lower()):
                self.destroy()

    def new_scene(self):
        self.token += 1
        self.canvas.configure(bg=BG_HAUT, cursor="")
        self.canvas.delete("all")
        self.canvas.unbind("<Motion>")
        return self.token

    def _fond(self, w, h):
        bandes = 64
        for i in range(bandes):
            col = melange(BG_HAUT, BG_BAS, i / (bandes - 1))
            self.canvas.create_rectangle(0, h * i / bandes, w, h * (i + 1) / bandes + 1,
                                         fill=col, outline=col)

    @staticmethod
    def _build_track(w, h, steps_spec, margin):
        x0, x1, y0, y1 = margin, w - margin, margin, h - margin
        usable_w, usable_h = x1 - x0, y1 - y0
        cx, cy = x0, y0
        first_w = steps_spec[0][2]
        start_box = (int(x0), int(y0), int(x0 + first_w), int(y0 + first_w))
        clamp = lambda a, b, c, d: (max(0, a), max(0, b), min(w, c), min(h, d))
        PAD = DOT_R + 3
        safe = []
        for axis, frac, width in steps_spec:
            if axis == "H":
                length = usable_w * frac
                a, b = min(cx, cx + length) - width, max(cx, cx + length) + width
                safe.append(clamp(a, cy - PAD, b, cy + width + PAD))
                cx += length
            else:
                length = usable_h * frac
                a, b = min(cy, cy + length) - width, max(cy, cy + length) + width
                safe.append(clamp(cx - PAD, a, cx + width + PAD, b))
                cy += length
        half = steps_spec[-1][2] / 2
        goal_box = clamp(cx - half - PAD, cy - half - PAD, cx + half + PAD, cy + half + PAD)
        return [tuple(map(int, r)) for r in safe], start_box, tuple(map(int, goal_box))

    def _peindre_piste(self, safe):
        for grow, col in HALO:
            for a, b, c, d in safe:
                self.canvas.create_rectangle(a - grow, b - grow, c + grow, d + grow,
                                             fill=col, outline=col)
        for a, b, c, d in safe:
            self.canvas.create_rectangle(a, b, c, d, fill=CORRIDOR, outline=CORRIDOR)

    def _pastille(self, box, couleur, label):
        for grow, t in ((11, 0.12), (7, 0.26), (4, 0.5)):
            col = melange(CORRIDOR, couleur, t)
            self.canvas.create_rectangle(box[0] - grow, box[1] - grow,
                                         box[2] + grow, box[3] + grow,
                                         fill=col, outline=col)
        self.canvas.create_rectangle(*box, fill=couleur, outline=couleur)
        self.canvas.create_text((box[0] + box[2]) / 2, box[3] + 24, text=label,
                                font=HUD, fill=melange(ENCRE, couleur, 0.5))

    def carte_niveau(self, niveau):
        if niveau >= len(NIVEAUX):
            return self.fin()
        t = self.new_scene()
        w, h = self.canvas.winfo_width(), self.canvas.winfo_height()
        self._fond(w, h)
        self.canvas.create_text(w / 2, h / 2 - 18, text=f"NIVEAU {niveau + 1}",
                                font=TITRE, fill=NEON)
        largeur = NIVEAUX[niveau][0][2]
        self.canvas.create_text(w / 2, h / 2 + 34,
                                text=f"couloir de {largeur} px", font=SOUS, fill=ENCRE)
        self.after(700, lambda: self.laby_start(niveau) if t == self.token else None)

    def _secouer(self, t, i=0):
        if t != self.token or i >= len(SECOUSSE):
            return
        self.canvas.move("all", *SECOUSSE[i])
        self.after(26, lambda: self._secouer(t, i + 1))

    def _eclats(self, x, y, t, parts=None, frame=0):
        if t != self.token:
            return
        if parts is None:
            parts = []
            for _ in range(14):
                ang, spd = random.uniform(0, 6.28), random.uniform(2.5, 7.5)
                it = self.canvas.create_oval(x - 3, y - 3, x + 3, y + 3,
                                             fill=ROUGE, outline="")
                parts.append([it, x, y, math.cos(ang) * spd, math.sin(ang) * spd])
        if frame >= 11:
            for it, *_ in parts:
                self.canvas.delete(it)
            return
        col = melange(ROUGE, CORRIDOR, frame / 11)
        r = max(1.0, 3.5 * (1 - frame / 11))
        for p in parts:
            p[1] += p[3]
            p[2] += p[4]
            p[4] += 0.45
            self.canvas.coords(p[0], p[1] - r, p[2] - r, p[1] + r, p[2] + r)
            self.canvas.itemconfigure(p[0], fill=col)
        self.after(28, lambda: self._eclats(x, y, t, parts, frame + 1))

    def fin(self):
        t = self.new_scene()
        w, h = self.canvas.winfo_width(), self.canvas.winfo_height()
        self._fond(w, h)
        duree = int(time.time() - self.t0) if self.t0 else 0
        self.canvas.create_text(w / 2, h / 2 - 30, text="TERMINÉ", font=TITRE, fill=DEPART)
        self.canvas.create_text(w / 2, h / 2 + 26,
                                text=f"{duree // 60:02d}:{duree % 60:02d}   —   "
                                     f"{self.morts} erreur{'s' if self.morts > 1 else ''}",
                                font=SOUS, fill=ENCRE)
        self.after(2200, self.destroy)

    def laby_start(self, niveau):
        t = self.new_scene()
        w, h = self.canvas.winfo_width(), self.canvas.winfo_height()
        if niveau >= len(NIVEAUX):
            return self.fin()

        self._fond(w, h)
        safe, start_box, goal_box = self._build_track(w, h, NIVEAUX[niveau], MARGES[niveau])
        self._peindre_piste(safe)
        self._pastille(start_box, DEPART, "DÉPART")
        self._pastille(goal_box, ARRIVEE, "ARRIVÉE")

        self.canvas.create_text(28, 26, text=f"NIVEAU {niveau + 1}/{len(NIVEAUX)}",
                                font=HUD, fill=NEON, anchor="w")
        hud = self.canvas.create_text(w - 28, 26, text="", font=HUD, fill=ENCRE, anchor="e")

        self.canvas.configure(cursor="none")
        self.canvas.event_generate("<Motion>", warp=True, x=w // 2, y=h // 2)
        cx0 = (start_box[0] + start_box[2]) / 2
        cy0 = (start_box[1] + start_box[3]) / 2
        state = {"vx": cx0, "vy": cy0, "raw": None}

        trail = []
        for i in range(TRAIL_N):
            col = melange(CORRIDOR, NEON, ((i + 1) / TRAIL_N) ** 2 * 0.75)
            trail.append(self.canvas.create_oval(0, 0, 0, 0, fill=col, outline=""))
        halo = self.canvas.create_oval(0, 0, 0, 0, fill=melange(CORRIDOR, NEON, 0.45),
                                       outline="")
        dot = self.canvas.create_oval(0, 0, 0, 0, fill=POINT, outline="")

        def placer():
            x, y = state["vx"], state["vy"]
            self.canvas.coords(halo, x - DOT_R - 5, y - DOT_R - 5,
                               x + DOT_R + 5, y + DOT_R + 5)
            self.canvas.coords(dot, x - DOT_R, y - DOT_R, x + DOT_R, y + DOT_R)
            hist = state.setdefault("hist", [])
            hist.append((x, y))
            del hist[:-TRAIL_N]
            for i, it in enumerate(trail):
                if i < len(hist):
                    hx, hy = hist[i]
                    r = DOT_R * (0.3 + 0.6 * (i + 1) / TRAIL_N)
                    self.canvas.coords(it, hx - r, hy - r, hx + r, hy + r)
                else:
                    self.canvas.coords(it, 0, 0, 0, 0)

        placer()

        def tic():
            if t != self.token:
                return
            d = int(time.time() - self.t0)
            self.canvas.itemconfigure(
                hud, text=f"{d // 60:02d}:{d % 60:02d}   ✕ {self.morts}")
            self.after(250, tic)

        tic()

        _in = lambda box, x, y: box[0] <= x <= box[2] and box[1] <= y <= box[3]
        on_track = lambda px, py: (_in(start_box, px, py) or _in(goal_box, px, py)
                                   or any(_in(r, px, py) for r in safe))

        def check_point(px, py):
            if _in(goal_box, px, py):
                return "goal"
            if not on_track(px, py):
                return "wall"
            for cos_a, sin_a in RING:
                if not on_track(px + DOT_R * cos_a, py + DOT_R * sin_a):
                    return "wall"
            return "safe"

        def reset_dot(px, py):
            self.morts += 1
            self._eclats(px, py, t)
            self._secouer(t)
            state["vx"], state["vy"] = cx0, cy0
            state["hist"] = []
            self.canvas.delete("flash")
            flash = self.canvas.create_rectangle(0, 0, w, h, fill=ROUGE, outline="",
                                                 stipple="gray25", tags="flash")
            self.after(110, lambda: self.canvas.delete(flash))

        def move(e):
            if t != self.token:
                return
            if state["raw"] is None:
                state["raw"] = (e.x, e.y)
                return
            lx, ly = state["raw"]
            state["raw"] = (e.x, e.y)
            dx, dy = e.x - lx, e.y - ly

            if not (MARGE_RECENTRE < e.x < w - MARGE_RECENTRE
                    and MARGE_RECENTRE < e.y < h - MARGE_RECENTRE):
                self.canvas.event_generate("<Motion>", warp=True, x=w // 2, y=h // 2)
                # Caler la référence sur le centre : les événements que le warp
                # provoque donnent alors un déplacement nul au lieu d'un saut.
                state["raw"] = (w // 2, h // 2)

            dx, dy = dx * SENS, dy * SENS
            ovx, ovy = state["vx"], state["vy"]
            nvx, nvy = ovx + dx, ovy + dy

            steps_n = min(400, max(1, int(max(abs(dx), abs(dy)) / 3)))
            result, hit = "safe", (nvx, nvy)
            for i in range(1, steps_n + 1):
                px, py = ovx + (nvx - ovx) * i / steps_n, ovy + (nvy - ovy) * i / steps_n
                r = check_point(px, py)
                if r in ("goal", "wall"):
                    result, hit = r, (px, py)
                    break

            if result == "goal":
                return self.carte_niveau(niveau + 1)
            if result == "wall":
                reset_dot(*hit)
            else:
                state["vx"], state["vy"] = nvx, nvy
            placer()

        self.canvas.bind("<Motion>", move)


if __name__ == "__main__":
    couper_raccourcis()
    atexit.register(restaurer_raccourcis)
    try:
        Jeu().mainloop()
    finally:
        restaurer_raccourcis()
