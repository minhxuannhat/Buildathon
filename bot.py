"""Improved Mech Royale bot for mechfight/1."""

import json
import math
import os
import random
import mechfight as mf

PICK_ORDER = [
    "armor", "plating", "nanites", "servos",
    "gun_damage", "gun_rate", "gun_mag",
    "laser_damage", "laser_charge", "lightning_efficiency", "mortar_reload",
    "sensors", "gyros", "reactor", "capacitor", "jets",
]
WEAPON_PREF = {"laser": 4, "mortar": 3, "lightning": 2, "gun": 1}
BOUNDS = (-512.0, 555.0, -333.0, 388.0)
LOUD = ("gun", "laser", "laser_charge", "lightning", "mortar", "explosion")


def wrap(a):
    return (a + math.pi) % (2 * math.pi) - math.pi


def unit(x, y):
    n = math.hypot(x, y)
    return (x / n, y / n) if n > 1e-6 else (0.0, 0.0)


def point_in_poly(x, y, poly):
    n = len(poly) // 2
    if n < 3:
        return False
    inside = False
    j = n - 1
    for i in range(n):
        xi, yi = poly[2 * i], poly[2 * i + 1]
        xj, yj = poly[2 * j], poly[2 * j + 1]
        if ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / ((yj - yi) or 1e-9) + xi):
            inside = not inside
        j = i
    return inside


def seg_intersects_poly(x1, y1, x2, y2, poly):
    if point_in_poly(0.5 * (x1 + x2), 0.5 * (y1 + y2), poly):
        return True
    n = len(poly) // 2
    if n < 2:
        return False

    def ccw(ax, ay, bx, by, cx, cy):
        return (cy - ay) * (bx - ax) > (by - ay) * (cx - ax)

    for i in range(n):
        j = (i + 1) % n
        ax, ay = poly[2 * i], poly[2 * i + 1]
        bx, by = poly[2 * j], poly[2 * j + 1]
        if (
            ccw(x1, y1, ax, ay, bx, by) != ccw(x2, y2, ax, ay, bx, by)
            and ccw(x1, y1, x2, y2, ax, ay) != ccw(x1, y1, x2, y2, bx, by)
        ):
            return True
    return False


class MapHelper:
    def __init__(self, map_info):
        self.prisms = []
        if not map_info:
            return
        mdir = map_info.get("dir")
        files = map_info.get("files") or []
        wpath = None
        for f in files:
            if f.endswith("world.json"):
                wpath = os.path.join(mdir, f) if mdir else f
                break
        if not wpath and mdir:
            cand = os.path.join(mdir, "v2", "world.json")
            if os.path.exists(cand):
                wpath = cand
        if wpath and os.path.exists(wpath):
            try:
                with open(wpath, "r", encoding="utf-8") as fp:
                    data = json.load(fp)
                for p in data.get("prisms") or []:
                    if p.get("kind") in ("building", "stadium", "bridge"):
                        ring = p.get("ring") or []
                        if len(ring) >= 6:
                            xs = ring[0::2]
                            ys = ring[1::2]
                            self.prisms.append({
                                "kind": p.get("kind"),
                                "base": float(p.get("base", -1e9)),
                                "top": float(p.get("top", 1e9)),
                                "ring": ring,
                                "holes": p.get("holes") or [],
                                "minx": min(xs),
                                "maxx": max(xs),
                                "miny": min(ys),
                                "maxy": max(ys),
                            })
            except Exception:
                self.prisms = []

    def _blocks_at(self, x, y, z, pad=2.5):
        for pr in self.prisms:
            if pr["kind"] == "bridge" and z + 1.2 < pr["base"]:
                continue
            if z > pr["top"] or z + 4.5 < pr["base"]:
                continue
            if x < pr["minx"] - pad or x > pr["maxx"] + pad or y < pr["miny"] - pad or y > pr["maxy"] + pad:
                continue
            if point_in_poly(x, y, pr["ring"]) and not any(point_in_poly(x, y, h) for h in pr["holes"]):
                return pr
        return None

    def has_los_2d(self, a, b, z_mid=3.5):
        if not self.prisms:
            return True
        x1, y1 = a[0], a[1]
        x2, y2 = b[0], b[1]
        sminx, smaxx = min(x1, x2), max(x1, x2)
        sminy, smaxy = min(y1, y2), max(y1, y2)
        for pr in self.prisms:
            if pr["kind"] == "bridge" and z_mid < pr["base"]:
                continue
            if pr["top"] < z_mid - 2.0 or pr["base"] > z_mid + 4.0:
                continue
            if pr["maxx"] < sminx or pr["minx"] > smaxx or pr["maxy"] < sminy or pr["miny"] > smaxy:
                continue
            if seg_intersects_poly(x1, y1, x2, y2, pr["ring"]):
                if not any(point_in_poly(0.5 * (x1 + x2), 0.5 * (y1 + y2), h) for h in pr["holes"]):
                    return False
        return True

    def steer_move(self, x, y, z, vx, vy, pref_sign=1):
        """Route tangentially if a forward probe hits a blocking prism."""
        n = math.hypot(vx, vy)
        if n < 1e-6 or not self.prisms:
            return vx, vy, False
        ux, uy = vx / n, vy / n
        # Probe 6m and 12m ahead
        hit_prism = None
        for dist in (6.0, 12.0):
            pr = self._blocks_at(x + ux * dist, y + uy * dist, z, pad=2.5)
            if pr is not None:
                hit_prism = pr
                break
        if hit_prism is None:
            return vx, vy, False

        # Try left/right tangential deflections
        for sgn in (pref_sign, -pref_sign):
            for ang in (sgn * 0.85, sgn * 1.35):
                ca, sa = math.cos(ang), math.sin(ang)
                tx = ux * ca - uy * sa
                ty = ux * sa + uy * ca
                if self._blocks_at(x + tx * 8.0, y + ty * 8.0, z, pad=2.0) is None:
                    return tx * n, ty * n, (hit_prism["top"] - z) < 28.0

        # Fallback push away from prism center
        cx = 0.5 * (hit_prism["minx"] + hit_prism["maxx"])
        cy = 0.5 * (hit_prism["miny"] + hit_prism["maxy"])
        ax, ay = unit(x - cx, y - cy)
        return (ux * 0.4 + ax * 1.2) * n, (uy * 0.4 + ay * 1.2) * n, (hit_prism["top"] - z) < 28.0


def lead(src, tpos, tvel, speed, iters=3):
    px, py, pz = tpos
    for _ in range(iters):
        d = math.dist((px, py, pz), src)
        t = d / max(speed, 1.0)
        px = tpos[0] + tvel[0] * t
        py = tpos[1] + tvel[1] * t
        pz = tpos[2] + tvel[2] * t
    return px, py, pz


def mortar_pitch(src, tpos, tvel, v, g):
    px, py, pz = tpos
    th = 0.7
    for _ in range(3):
        x = math.hypot(px - src[0], py - src[1])
        y = pz - src[2]
        disc = v ** 4 - g * (g * x * x + 2 * y * v * v)
        if disc < 0 or x < 1:
            return None
        th = math.atan2(v * v - math.sqrt(disc), g * x)
        t = x / (v * max(math.cos(th), 0.2))
        px = tpos[0] + tvel[0] * t
        py = tpos[1] + tvel[1] * t
        pz = tpos[2] + tvel[2] * t
    return math.atan2(py - src[1], px - src[0]), th


class Bot:
    name = "survivor-v3"

    def __init__(self, hello):
        self.rng = random.Random(hello.get("seed", 0))
        cfg = hello["config"]
        self.g = cfg["weapons"]["mortar"].get("gravity", 22)
        self.pickup_r = cfg["loot"]["pickup_radius"]
        self.map = MapHelper(hello.get("map"))
        self.strafe = 1
        self.strafe_until = 0.0
        self.hit_bearing = None
        self.hit_t = -99.0
        self.stuck = 0
        self.escape_until = -1.0
        self.escape_dir = 1
        self.last_cmd = 0.0
        self.scan_dir = 1
        self.black = {}
        self.farm_id = None
        self.farm_t0 = 0.0
        self.ang = (hello.get("slot", 0) * 2 * math.pi / 16) + self.rng.uniform(-0.25, 0.25)

    @staticmethod
    def eff_circle(z, t):
        if t <= z["shrink_start"]:
            return z["center"], z["radius"]
        if t >= z["shrink_end"]:
            return z["next_center"], z["next_radius"]
        f = (t - z["shrink_start"]) / max(z["shrink_end"] - z["shrink_start"], 1e-6)
        c = [z["center"][i] + (z["next_center"][i] - z["center"][i]) * f for i in (0, 1)]
        return c, z["radius"] + (z["next_radius"] - z["radius"]) * f

    @staticmethod
    def _ok(a):
        f = lambda v: isinstance(v, (int, float)) and math.isfinite(v)
        return (
            len(a.get("move", [0, 0])) == 2
            and all(f(v) for v in a.get("move", [0, 0]))
            and len(a.get("aim", [0, 0])) == 2
            and all(f(v) for v in a.get("aim", [0, 0]))
            and len(a.get("fire", [False, False])) == 2
        )

    def act(self, obs):
        try:
            a = self._act(obs)
            if not self._ok(a):
                raise ValueError("invalid action %r" % (a,))
            return a
        except Exception:
            import traceback
            mf.log(traceback.format_exc())
            return self._fallback(obs)

    def _fallback(self, obs):
        me = obs["self"]
        x, y, _ = me["pos"]
        act = {"tick": obs["tick"], "fire": [False, False]}
        en = [m for m in obs["mechs"] if m["kind"] == "mech"]
        z = obs["zone"]
        gx, gy = unit(z["next_center"][0] - x, z["next_center"][1] - y)
        act["move"] = [gx, gy]
        if en:
            m = min(en, key=lambda m: math.dist(m["pos"][:2], (x, y)))
            act["aim"] = [math.atan2(m["pos"][1] - y, m["pos"][0] - x), 0.0]
            act["fire"] = [True, False]
        return act

    def _pick_mech_target(self, x, y, z, enemies):
        """Prefer closer, lower-HP, or already-engaged mechs within 200m."""
        best_m = None
        best_d = 1e9
        best_score = 1e9
        for d, m in enemies:
            if d > 200:
                continue
            hp = float(m.get("hp", 1400.0))
            max_hp = max(1.0, float(m.get("max_hp", 1400.0)))
            hp_ratio = hp / max_hp
            los_pen = 0.0 if self.map.has_los_2d((x, y), m["pos"][:2], z_mid=z + 4.5) else 35.0
            score = d * (0.75 + 0.4 * hp_ratio) + los_pen
            if score < best_score:
                best_score = score
                best_m = m
                best_d = d
        return best_m, best_d

    def _act(self, obs):
        me = obs["self"]
        t = obs["t"]
        x, y, z = me["pos"]
        stats = me["stats"]
        walk = stats.get("walk_speed", 11.7)
        mz = z + stats.get("muzzle_height", 7)
        cz = z + stats.get("height", 9) * 0.5
        act = {"tick": obs["tick"]}

        if obs["offer"]:
            ids = [c["id"] for c in obs["offer"]["choices"]]
            act["pick"] = min(
                range(len(ids)),
                key=lambda i: PICK_ORDER.index(ids[i]) if ids[i] in PICK_ORDER else 99,
            )

        for e in obs["events"]:
            if e["type"] == "damaged" and e.get("bearing") is not None:
                self.hit_bearing, self.hit_t = e["bearing"], t

        enemies = []
        for m in obs["mechs"]:
            if m["kind"] != "mech":
                continue
            d = math.dist(m["pos"][:2], (x, y))
            enemies.append((d, m))
        enemies.sort(key=lambda e: e[0])

        near = [e for e in enemies if e[0] < 100]
        tgt, tdist = self._pick_mech_target(x, y, z, enemies)
        fth = 4.5

        zc0, zr0 = self.eff_circle(obs["zone"], t)
        late = t >= 150 or zr0 <= 140
        mid = t >= 70
        flim = 0 if late else (50 if mid else 115)
        lootlim = 25 if late else (65 if mid else 115)
        cubelim = 15 if late else 35

        if flim > 0 and tgt is None and me["in_zone"]:
            ftgt = None
            best = 1e9
            cands = (
                [(c, 1.0, 1.5) for c in obs["crates"]]
                + [(m, 1.5, 1.8) for m in obs["mechs"] if m["kind"] == "npc"]
            )
            for c, pen, h in cands:
                cid = c["id"]
                if self.black.get(cid, -1) > t:
                    continue
                d = math.dist(c["pos"][:2], (x, y))
                if d > flim or math.dist(c["pos"][:2], zc0) > zr0 * 0.85:
                    continue
                if not self.map.has_los_2d((x, y), c["pos"][:2], z_mid=z + 2.0):
                    continue
                if d * pen < best:
                    best, ftgt, fth = d * pen, c, h
            if ftgt is not None:
                if self.farm_id != ftgt["id"]:
                    self.farm_id, self.farm_t0 = ftgt["id"], t
                elif t - self.farm_t0 > 12:
                    self.black[ftgt["id"]] = t + 35
                    ftgt = None
                if ftgt is not None:
                    tgt = {"pos": ftgt["pos"], "vel": ftgt.get("vel", [0, 0, 0]), "kind": "farm"}
                    tdist = math.dist(ftgt["pos"][:2], (x, y))

        retreat = False
        if near:
            m0 = near[0][1]
            ehp = m0.get("hp", me["hp"])
            retreat = len(near) >= 3 or me["hp"] < 0.35 * me["max_hp"] or me["hp"] < 0.5 * ehp

        # Zone goal
        zn = obs["zone"]
        cc, cr = self.eff_circle(zn, t)
        fc, fr = self.eff_circle(zn, t + 12.0)
        rr = max(fr * (0.55 if late else 0.72), 5.0)
        gx = fc[0] + math.cos(self.ang) * rr
        gy = fc[1] + math.sin(self.ang) * rr
        gx = min(max(gx, BOUNDS[0] + 40), BOUNDS[1] - 40)
        gy = min(max(gy, BOUNDS[2] + 40), BOUNDS[3] - 40)
        ex, ey = gx - fc[0], gy - fc[1]
        ed = math.hypot(ex, ey)
        if ed > rr:
            gx, gy = fc[0] + ex / ed * rr, fc[1] + ey / ed * rr

        dist_goal = math.hypot(gx - x, gy - y)
        cd = math.hypot(x - cc[0], y - cc[1])
        out_soon = cd > cr * 0.92
        urgent = (not me["in_zone"]) or out_soon or (dist_goal / max(walk, 1.0) * 1.6 > max(zn["shrink_end"] - t, 0.2))

        vx = vy = 0.0
        if dist_goal > (12.0 if late else 25.0):
            ux, uy = unit(gx - x, gy - y)
            vx += ux
            vy += uy
        elif late:
            self.ang = wrap(self.ang + 0.18)
            ux, uy = unit(math.cos(self.ang), math.sin(self.ang))
            vx += ux * 0.6
            vy += uy * 0.6

        if tgt and tgt.get("kind") == "farm":
            vx = vy = 0.0
            if tdist > 30:
                ux, uy = unit(tgt["pos"][0] - x, tgt["pos"][1] - y)
                vx, vy = ux, uy

        # Loot & cubes
        quiet = not near
        calm = not [e for e in near if e[0] < 65]
        want = None
        if calm and me["weapons"][1] is None:
            best_score = -1e9
            for p in obs["pickups"]:
                pref = WEAPON_PREF.get(p["kind"], 0)
                if pref <= 1:
                    continue
                if math.dist(p["pos"][:2], cc) > cr * 0.85:
                    continue
                d = math.dist(p["pos"][:2], (x, y))
                sc = pref * 35 - d
                if d < lootlim and sc > best_score:
                    best_score, want = sc, p
        if want:
            if math.dist(want["pos"][:2], (x, y)) < self.pickup_r - 0.25 and abs(want["pos"][2] - z) < 3.6:
                act["take"] = want["id"]
            ux, uy = unit(want["pos"][0] - x, want["pos"][1] - y)
            vx, vy = vx * 0.25 + ux * 1.5, vy * 0.25 + uy * 1.5
            urgent = True
        elif quiet:
            for c in obs["cubes"]:
                if math.dist(c["pos"][:2], (x, y)) < cubelim and math.dist(c["pos"][:2], cc) < cr * 0.85:
                    ux, uy = unit(c["pos"][0] - x, c["pos"][1] - y)
                    vx += ux * 0.9
                    vy += uy * 0.9
                    break

        # Enemy spacing & strafe
        if near:
            if t > self.strafe_until:
                self.strafe = self.rng.choice((-1, 1))
                self.strafe_until = t + self.rng.uniform(0.7, 1.6)
            d0, m0 = near[0]
            have_l = any(w and w["kind"] == "lightning" for w in me["weapons"])
            lo, hi = (16, 34) if (have_l and not retreat) else (30, 58)
            ax, ay = unit(x - m0["pos"][0], y - m0["pos"][1])
            if retreat:
                vx += ax * 1.6
                vy += ay * 1.6
            elif d0 < lo:
                vx += ax * 0.55
                vy += ay * 0.55
            elif d0 > hi:
                vx -= ax * 0.75
                vy -= ay * 0.75
            vx += -ay * self.strafe * 0.95
            vy += ax * self.strafe * 0.95
            for _, m in near[1:]:
                bx, by = unit(x - m["pos"][0], y - m["pos"][1])
                vx += bx * 0.5
                vy += by * 0.5
            if retreat:
                urgent = True

        # Dodge projectiles & charging lasers
        dodge = 0
        for p in obs["projectiles"]:
            r = [p["pos"][i] - me["pos"][i] for i in range(2)] + [p["pos"][2] - cz]
            v = p["vel"]
            vv = v[0] ** 2 + v[1] ** 2 + v[2] ** 2
            if vv < 1:
                continue
            tc = -(r[0] * v[0] + r[1] * v[1] + r[2] * v[2]) / vv
            if not 0 < tc < 1.2:
                continue
            cx_, cy_ = r[0] + v[0] * tc, r[1] + v[1] * tc
            cz_ = r[2] + v[2] * tc
            if math.sqrt(cx_ * cx_ + cy_ * cy_ + cz_ * cz_) > 7.5:
                continue
            px_, py_ = unit(-v[1], v[0])
            if px_ * cx_ + py_ * cy_ > 0:
                px_, py_ = -px_, -py_
            vx += px_ * 2.1
            vy += py_ * 2.1
            dodge += 1

        for d, m in enemies:
            if d < 220 and m.get("charging"):
                ang = math.atan2(y - m["pos"][1], x - m["pos"][0])
                if abs(wrap(ang - m["aim"][0])) < 0.35:
                    px_, py_ = unit(-math.sin(m["aim"][0]), math.cos(m["aim"][0]))
                    s = 1 if self.strafe > 0 else -1
                    vx += px_ * s * 2.1
                    vy += py_ * s * 2.1
                    dodge += 1
        if dodge:
            urgent = True

        # Prism steering + jet-climb assist + stuck recovery
        vx, vy, climb_hint = self.map.steer_move(x, y, z, vx, vy, pref_sign=self.strafe)

        speed = math.hypot(me["vel"][0], me["vel"][1])
        if self.last_cmd > 0.5 and speed < 1.5 and me["grounded"]:
            self.stuck += 1
        else:
            self.stuck = max(0, self.stuck - 1)
        if self.stuck > 6 and t > self.escape_until:
            self.escape_until = t + 1.4
            self.escape_dir = self.rng.choice((-1, 1))
            self.stuck = 0

        jump = False
        if t < self.escape_until and (vx or vy):
            ang = math.atan2(vy, vx) + self.escape_dir * math.pi / 2
            n = math.hypot(vx, vy)
            vx, vy = math.cos(ang) * n, math.sin(ang) * n
            jump = True
            urgent = True
        elif climb_hint and me["grounded"] and me["energy"] > 35:
            jump = True

        n = math.hypot(vx, vy)
        if n < 0.05:
            mv, frac = [0.0, 0.0], 0.0
        else:
            ux, uy = vx / n, vy / n
            frac = 1.0 if (urgent or dist_goal > 30) else 0.55
            mv = [ux * frac, uy * frac]
        self.last_cmd = frac
        act["move"] = mv
        act["jump"] = jump
        act["boost"] = bool(
            frac
            and (
                (not me["in_zone"] and me["energy"] > 15)
                or (retreat and me["energy"] > 45)
                or (t < self.escape_until and me["energy"] > 12)
                or (climb_hint and not me["grounded"] and me["energy"] > 30)
            )
        )

        # Aim & fire
        fire = [False, False]
        yaw_now, pitch_now = me["aim"]
        aim = None

        if tgt:
            tp = tgt["pos"]
            tv = tgt.get("vel", [0, 0, 0])
            tc = [tp[0], tp[1], tp[2] + fth]
            has_los = self.map.has_los_2d((x, y), tp[:2], z_mid=z + 4.5)
            slots = {w["kind"]: i for i, w in enumerate(me["weapons"]) if w}
            gunw = me["weapons"][slots["gun"]] if "gun" in slots else None
            tgt_has_mortar = "mortar" in (tgt.get("weapons") or [])

            use_mortar = (
                tgt.get("kind") != "farm"
                and "mortar" in slots
                and (
                    (not has_los and 25 < tdist < 225)
                    or (70 < tdist < 220)
                    or ("gun" not in slots and 22 < tdist < 220)
                )
            )
            if use_mortar:
                w = me["weapons"][slots["mortar"]]
                sol = mortar_pitch(
                    [x, y, z + stats.get("height", 9) + 0.5],
                    tc, tv, w["stats"].get("speed", 72), self.g,
                )
                if sol:
                    aim = [
                        sol[0],
                        max(stats.get("min_pitch", -0.7), min(stats.get("max_pitch", 1.3), sol[1])),
                    ]
                    if abs(wrap(aim[0] - yaw_now)) < 0.08 and abs(aim[1] - pitch_now) < 0.08 and w["cooldown"] <= 0.1:
                        fire[slots["mortar"]] = True

            if aim is None:
                spd = gunw["stats"].get("speed", 252) if gunw else 252
                px, py, pz = lead([x, y, mz], tc, tv, spd)
                hd = math.hypot(px - x, py - y)
                aim = [math.atan2(py - y, px - x), math.atan2(pz - mz, max(hd, 0.1))]
                tol = max(0.06, min(0.3, 1.6 * math.atan2(2.5, max(tdist, 1.0))))
                aligned = abs(wrap(aim[0] - yaw_now)) < tol and abs(aim[1] - pitch_now) < tol * 1.5
                ydir = math.atan2(tc[1] - y, tc[0] - x)

                for kind, i in slots.items():
                    w = me["weapons"][i]
                    st = w["stats"]
                    if kind == "gun":
                        if has_los and aligned and tdist < min(st.get("range", 230), 190) and w.get("ammo", 1) > 0:
                            fire[i] = True
                        elif w.get("ammo", 1) == 0 and not w.get("reload"):
                            act["reload"] = True
                    elif kind == "lightning":
                        cone = math.radians(st.get("cone_deg", 30)) / 2 * 0.85
                        if has_los and tdist < st.get("range", 45) * 0.95 and abs(wrap(ydir - yaw_now)) < cone and me["energy"] > 8:
                            fire[i] = True
                    elif kind == "laser":
                        ch = w.get("charge", 0.0)
                        full = st.get("charge", 0.75)
                        min_ch = full * (0.55 if tgt_has_mortar else 0.85)
                        if has_los and tdist < st.get("range", 280) * 0.92 and w["cooldown"] <= 0.2:
                            if ch >= min_ch and aligned:
                                fire[i] = False
                            else:
                                fire[i] = True
                        elif ch > 0 and aligned:
                            fire[i] = False
                        elif ch > 0:
                            fire[i] = True
        else:
            hear = [s for s in obs["sounds"] if s["kind"] in LOUD and s["dist"] != "far"]
            if hear:
                aim = [hear[0]["bearing"], 0.0]
            elif self.hit_bearing is not None and t - self.hit_t < 2.5:
                aim = [self.hit_bearing, 0.0]
            else:
                if abs(wrap(yaw_now - (self.ang + 0.6))) > 0.65:
                    self.scan_dir *= -1
                aim = [wrap(yaw_now + self.scan_dir * 0.28), 0.0]

            w0 = me["weapons"][0]
            if w0 and w0["kind"] == "gun" and w0.get("ammo", 99) < w0["stats"].get("mag", 12) * 0.5 and not w0.get("reload"):
                act["reload"] = True

        act["aim"] = aim
        act["fire"] = fire
        mf.draw.label(f"hp {me['hp']:.0f} {'R' if retreat else ''} e{len(near)}")
        return act


mf.run(Bot)
