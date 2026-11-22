"""Mech Royale bot v6: rooftop survival.

Playtest findings drove this version: bots hit a mech on a rooftop poorly, aim/legs lag (slew + accel),
and the safest place is near the zone edge. So: climb a reachable roof inside the safe circle and hold it,
fight from there, flee/hide when outmatched, and handle low HP / group fights with explicit rules.
"""
import json
import math
import os
import random
import mechfight as mf

PICK_ORDER = ["armor", "plating", "nanites", "servos", "gun_damage", "gun_rate", "gun_mag", "laser_damage",
              "laser_charge", "lightning_efficiency", "mortar_reload", "sensors", "gyros", "reactor",
              "capacitor", "jets"]
WEAPON_PREF = {"laser": 3, "lightning": 2.5, "mortar": 1}
BOUNDS = (-512.0, 555.0, -333.0, 388.0)  # minX, maxX, minY, maxY
LOUD = ("gun", "laser", "laser_charge", "lightning", "mortar", "explosion")


def wrap(a):
    return (a + math.pi) % (2 * math.pi) - math.pi


def unit(x, y):
    n = math.hypot(x, y)
    return (x / n, y / n) if n > 1e-6 else (0.0, 0.0)


def lead(src, tpos, tvel, speed, iters=3):
    px, py, pz = tpos
    for _ in range(iters):
        d = math.dist((px, py, pz), src)
        t = d / max(speed, 1.0)
        px, py, pz = tpos[0] + tvel[0] * t, tpos[1] + tvel[1] * t, tpos[2] + tvel[2] * t
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
        px, py, pz = tpos[0] + tvel[0] * t, tpos[1] + tvel[1] * t, tpos[2] + tvel[2] * t
    return math.atan2(py - src[1], px - src[0]), th


def ring_info(r, x, y):
    """(inside?, distance to the boundary) for a flat [x0,y0,x1,y1,...] polygon."""
    c = False
    best = 1e9
    n = len(r) // 2
    for i in range(n):
        x1, y1 = r[2 * i], r[2 * i + 1]
        j = (i + 1) % n
        x2, y2 = r[2 * j], r[2 * j + 1]
        if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / (y2 - y1 + 1e-12) + x1:
            c = not c
        dx, dy = x2 - x1, y2 - y1
        L = dx * dx + dy * dy
        t = 0.0 if L == 0 else max(0.0, min(1.0, ((x - x1) * dx + (y - y1) * dy) / L))
        best = min(best, math.hypot(x - (x1 + t * dx), y - (y1 + t * dy)))
    return c, best


class Bot:
    name = "roofer-py"

    def __init__(self, hello):
        self.rng = random.Random(hello.get("seed", 0))
        cfg = hello["config"]
        self.g = cfg["weapons"]["mortar"].get("gravity", 22)
        self.pickup_r = cfg["loot"]["pickup_radius"]
        self.strafe = 1
        self.strafe_until = 0.0
        self.hit_bearing = None
        self.hit_t = -99.0
        self.stuck = 0
        self.escape_until = -1.0
        self.escape_dir = 1
        self.last_cmd = 0.0
        self.scan = 0.0
        self.nlog = 0
        self.black = {}
        self.farm_id = None
        self.farm_t0 = 0.0
        self.ang = (hello.get("slot", 0) * 2 * math.pi / 16) + self.rng.uniform(-0.2, 0.2)
        self.roofs = []
        try:
            with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "roofs.json")) as f:
                self.roofs = json.load(f)
        except Exception as e:  # no roofs -> v5-style rim play
            mf.log("roofs.json not loaded:", e)
        self.roof = None       # index into self.roofs
        self.roof_check = -9.0
        self.climb_t0 = None
        self.on_roof = False
        self.last_roof_log = None

    # ---- zone ----
    @staticmethod
    def eff_circle(z, t):
        if t <= z["shrink_start"]:
            return z["center"], z["radius"]
        if t >= z["shrink_end"]:
            return z["next_center"], z["next_radius"]
        f = (t - z["shrink_start"]) / max(z["shrink_end"] - z["shrink_start"], 1e-6)
        c = [z["center"][i] + (z["next_center"][i] - z["center"][i]) * f for i in (0, 1)]
        return c, z["radius"] + (z["next_radius"] - z["radius"]) * f

    # ---- roofs ----
    def choose_roof(self, x, y, z, t, fc, fr, avoid=None):
        best, bi = 1e9, None
        for i, r in enumerate(self.roofs):
            if self.black.get(("roof", i), -1) > t:
                continue
            rise = r["top"] - z
            if rise < -2 or rise > 46:
                continue
            rc = math.hypot(r["x"] - fc[0], r["y"] - fc[1])
            if rc > fr * 0.8:
                continue
            d = math.hypot(r["x"] - x, r["y"] - y)
            if d > 260:
                continue
            score = d + 1.3 * max(rise, 0) + 0.3 * abs(rc - 0.7 * fr)
            if avoid is not None:  # keep away from the enemy we are fleeing
                score += max(0, 120 - math.hypot(r["x"] - avoid[0], r["y"] - avoid[1])) * 2
            if score < best:
                best, bi = score, i
        return bi

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

    @staticmethod
    def _ok(a):
        f = lambda v: isinstance(v, (int, float)) and math.isfinite(v)
        return (len(a.get("move", [0, 0])) == 2 and all(f(v) for v in a.get("move", [0, 0]))
                and len(a.get("aim", [0, 0])) == 2 and all(f(v) for v in a.get("aim", [0, 0]))
                and len(a.get("fire", [False, False])) == 2)

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

    def _act(self, obs):
        me = obs["self"]
        t = obs["t"]
        x, y, z = me["pos"]
        stats = me["stats"]
        walk = stats.get("walk_speed", 11.7)
        mz = z + stats.get("muzzle_height", 7)
        cz = z + stats.get("height", 9) * 0.5
        hp, mhp = me["hp"], me["max_hp"]
        act = {"tick": obs["tick"]}

        # ---- upgrades ----
        if obs["offer"]:
            ids = [c["id"] for c in obs["offer"]["choices"]]
            act["pick"] = min(range(len(ids)), key=lambda i: PICK_ORDER.index(ids[i]) if ids[i] in PICK_ORDER else 99)

        # ---- events ----
        for e in obs["events"]:
            if e["type"] == "damaged" and e.get("bearing") is not None:
                self.hit_bearing, self.hit_t = e["bearing"], t
                if self.nlog < 60:
                    self.nlog += 1
                    mf.log(f"t={t:.1f} hp={hp:.0f} hit {e['amount']:.0f} by {e.get('by')} {e['weapon']} "
                           f"rear={e.get('rear')} alive={obs['alive']} roof={self.on_roof} z={z:.0f}")
        recently_hit = t - self.hit_t < 2.0

        # ---- enemies ----
        enemies = sorted(((math.dist(m["pos"][:2], (x, y)), m) for m in obs["mechs"] if m["kind"] == "mech"),
                         key=lambda e: e[0])
        near = [e for e in enemies if e[0] < 100]

        def targets_me(m):
            d = math.dist(m["pos"][:2], (x, y))
            if d > 160:
                return False
            ang = math.atan2(y - m["pos"][1], x - m["pos"][0])
            return abs(wrap(ang - m["aim"][0])) < 0.45

        watch = [e for e in enemies if e[0] < 150]
        attackers = [e for e in watch if targets_me(e[1])]
        tgt = enemies[0][1] if enemies and enemies[0][0] < 200 else None
        tdist = enemies[0][0] if tgt else 1e9
        hide_mode = False
        if len(watch) >= 3 and not attackers and obs["alive"] > 3:   # rule 9: others are brawling
            hide_mode = True
            tgt, tdist = None, 1e9
        elif len(watch) == 2 and not attackers:                      # rule 8: two fighting each other
            hi = max(watch, key=lambda e: e[1].get("hp", 0))
            tgt, tdist = hi[1], hi[0]

        # ---- zone ----
        zn = obs["zone"]
        cc, cr = self.eff_circle(zn, t)
        zc0, zr0 = cc, cr
        cd = math.hypot(x - cc[0], y - cc[1])
        out_soon = cd > cr * 0.92
        zone_urgent = (not me["in_zone"]) or out_soon
        late = t >= 150 or zr0 <= 140
        fc, fr = self.eff_circle(zn, t + 12.0)

        # ---- low-HP / retreat rules (rules 4,5) ----
        low = hp < 0.4 * mhp
        allin = False
        approach = False
        retreat = False
        if near:
            d0, m0 = near[0]
            ehp = m0.get("hp", hp)
            if low:
                if d0 < 45:
                    allin = True                  # close: go all in
                elif hp > ehp:
                    approach = True               # far, and we are healthier: close the gap
                else:
                    retreat = True                # far, we are weaker: hide and recover
            else:
                retreat = len(near) >= 3 or hp < 0.5 * ehp
        if zone_urgent:
            retreat = False
            approach = False

        # ---- crates / drones for loot, early only ----
        flim = 0 if (t >= 45 or late) else 50
        lootlim = 25 if t >= 45 else 70
        cubelim = 12 if late else 25
        ftgt, fth = None, 4.5
        if flim > 0 and tgt is None and not [e for e in enemies if e[0] < 120] and me["in_zone"]:
            best = 1e9
            cands = [(c, 1.0, 1.5) for c in obs["crates"]] + \
                    [(m, 1.6, 1.8) for m in obs["mechs"] if m["kind"] == "npc"]
            for c, pen, h in cands:
                if self.black.get(c["id"], -1) > t:
                    continue
                d = math.dist(c["pos"][:2], (x, y))
                if d > flim or math.dist(c["pos"][:2], zc0) > zr0 * 0.85:
                    continue
                if d * pen < best:
                    best, ftgt, fth = d * pen, c, h
            if ftgt is not None:
                if self.farm_id != ftgt["id"]:
                    self.farm_id, self.farm_t0 = ftgt["id"], t
                elif t - self.farm_t0 > 14:
                    self.black[ftgt["id"]] = t + 40
                    ftgt = None
        if ftgt is not None:
            tgt = {"pos": ftgt["pos"], "vel": ftgt.get("vel", [0, 0, 0]), "kind": "farm"}
            tdist = math.dist(ftgt["pos"][:2], (x, y))

        # ---- goal: a roof inside the safe circle, else the rim ----
        goal_roof = None
        if self.roofs and not (ftgt is not None):
            if self.roof is not None:
                r = self.roofs[self.roof]
                bad = (self.black.get(("roof", self.roof), -1) > t or
                       math.hypot(r["x"] - fc[0], r["y"] - fc[1]) > fr * 0.85)
                if bad or (retreat and t - self.roof_check > 1.0 and not self.on_roof):
                    self.roof = None
            if self.roof is None and t - self.roof_check > 1.0:
                self.roof_check = t
                avoid = near[0][1]["pos"] if (retreat and near) else None
                self.roof = self.choose_roof(x, y, z, t, fc, fr, avoid)
                self.climb_t0 = None
            if self.roof is not None:
                goal_roof = self.roofs[self.roof]
        climbing = False
        self.on_roof = False
        if goal_roof is not None:
            gx, gy = goal_roof["x"], goal_roof["y"]
            inside, ed = ring_info(goal_roof["ring"], x, y)
            dgoal = math.hypot(gx - x, gy - y)
            if inside and abs(z - goal_roof["top"]) < 1.8 and me["grounded"]:
                self.on_roof = True
                self.climb_t0 = None
            elif (inside or ed < 5.0) and z < goal_roof["top"] - 0.2:
                climbing = True
                if self.climb_t0 is None:
                    self.climb_t0 = t
                if t - self.climb_t0 > 14 or (me["energy"] < 4 and not me["grounded"] and z < goal_roof["top"] - 6):
                    self.black[("roof", self.roof)] = t + 60
                    self.roof, self.climb_t0, climbing = None, None, False
                    goal_roof = None
            else:
                if self.climb_t0 is not None and t - self.climb_t0 > 14:
                    self.climb_t0 = None
        if goal_roof is None:
            # rim goal (v5)
            rr = max(fr * 0.72, 6.0)
            gx = fc[0] + math.cos(self.ang) * rr
            gy = fc[1] + math.sin(self.ang) * rr
            gx = min(max(gx, BOUNDS[0] + 40), BOUNDS[1] - 40)
            gy = min(max(gy, BOUNDS[2] + 40), BOUNDS[3] - 40)
            ex, ey = gx - fc[0], gy - fc[1]
            ed2 = math.hypot(ex, ey)
            if ed2 > rr:
                gx, gy = fc[0] + ex / ed2 * rr, fc[1] + ey / ed2 * rr
            if math.hypot(gx - x, gy - y) < 25:
                gx, gy = x, y
        dist_goal = math.hypot(gx - x, gy - y)
        urgent = zone_urgent or dist_goal / walk * 1.6 > (zn["shrink_end"] - t)
        vx = vy = 0.0
        if self.on_roof:
            if dist_goal > max(goal_roof["clr"] * 0.7, 3.0):
                ux, uy = unit(gx - x, gy - y)
                vx, vy = ux * 0.5, uy * 0.5
        elif dist_goal > 4:
            ux, uy = unit(gx - x, gy - y)
            vx, vy = ux, uy
        if ftgt is not None:
            vx = vy = 0.0
            if tdist > 32:
                ux, uy = unit(ftgt["pos"][0] - x, ftgt["pos"][1] - y)
                vx, vy = ux, uy

        # ---- loot (weapons; swap a mortar for a laser/lightning), cubes ----
        calm = not [e for e in near if e[0] < 60] and not recently_hit
        want, want_swap = None, False
        w1 = me["weapons"][1]
        can_swap = w1 is not None and w1["kind"] == "mortar"
        if calm and not zone_urgent and (w1 is None or can_swap) and not self.on_roof:
            best = -1
            for p in obs["pickups"]:
                if p["kind"] == "gun":
                    continue
                if can_swap and p["kind"] not in ("laser", "lightning"):
                    continue
                if any(w and w["kind"] == p["kind"] for w in me["weapons"]):
                    continue
                if math.dist(p["pos"][:2], cc) > cr * 0.85:
                    continue
                dist = math.dist(p["pos"][:2], (x, y))
                score = WEAPON_PREF.get(p["kind"], 0) * 30 - dist
                if dist < lootlim and score > best:
                    best, want, want_swap = score, p, can_swap
        if want:
            if math.dist(want["pos"][:2], (x, y)) < self.pickup_r - 0.3 and abs(want["pos"][2] - z) < 3.5:
                act["take"] = want["id"]
                if want_swap:
                    act["drop"] = 1
            ux, uy = unit(want["pos"][0] - x, want["pos"][1] - y)
            vx, vy = vx * 0.3 + ux * 1.5, vy * 0.3 + uy * 1.5
            urgent = True
        elif calm and not self.on_roof and not climbing:
            for c in obs["cubes"]:
                if math.dist(c["pos"][:2], (x, y)) < cubelim and math.dist(c["pos"][:2], cc) < cr * 0.85:
                    ux, uy = unit(c["pos"][0] - x, c["pos"][1] - y)
                    vx += ux * 0.9
                    vy += uy * 0.9
                    break

        # ---- enemy handling on the ground (skipped on a roof, when the zone is chasing us, or climbing) ----
        boost_extra = False
        if near and not self.on_roof and not climbing and not zone_urgent:
            if t > self.strafe_until:
                self.strafe = self.rng.choice((-1, 1))
                self.strafe_until = t + self.rng.uniform(1.0, 2.0)
            d0, m0 = near[0]
            ax, ay = unit(x - m0["pos"][0], y - m0["pos"][1])
            if approach:
                vx, vy = -ax * 1.5, -ay * 1.5
                boost_extra = True
                urgent = True
            elif retreat or hide_mode:
                vx += ax * 1.6
                vy += ay * 1.6
                urgent = True
            elif allin:
                vx, vy = -ay * self.strafe * 0.6, ax * self.strafe * 0.6
            else:
                if d0 < 30:
                    vx += ax * 0.5
                    vy += ay * 0.5
                elif d0 > 55:
                    vx -= ax * 0.8
                    vy -= ay * 0.8
                vx += -ay * self.strafe * 0.9
                vy += ax * self.strafe * 0.9
            for d, m in near[1:]:
                bx, by = unit(x - m["pos"][0], y - m["pos"][1])
                vx += bx * 0.5
                vy += by * 0.5

        # ---- dodge incoming shots (ground only) ----
        dodge = 0
        if not self.on_roof and not climbing:
            for p in obs["projectiles"]:
                r = [p["pos"][0] - x, p["pos"][1] - y, p["pos"][2] - cz]
                v = p["vel"]
                vv = v[0] ** 2 + v[1] ** 2 + v[2] ** 2
                if vv < 1:
                    continue
                tc = -(r[0] * v[0] + r[1] * v[1] + r[2] * v[2]) / vv
                if not 0 < tc < 1.2:
                    continue
                cx_, cy_, cz_ = r[0] + v[0] * tc, r[1] + v[1] * tc, r[2] + v[2] * tc
                if math.sqrt(cx_ * cx_ + cy_ * cy_ + cz_ * cz_) > 7:
                    continue
                px_, py_ = unit(-v[1], v[0])
                if px_ * cx_ + py_ * cy_ > 0:
                    px_, py_ = -px_, -py_
                vx += px_ * 2.0
                vy += py_ * 2.0
                dodge += 1
            for d, m in near:
                if m.get("charging"):
                    ang = math.atan2(y - m["pos"][1], x - m["pos"][0])
                    if abs(wrap(ang - m["aim"][0])) < 0.35:
                        px_, py_ = unit(-math.sin(m["aim"][0]), math.cos(m["aim"][0]))
                        s = 1 if self.strafe > 0 else -1
                        vx += px_ * s * 2.0
                        vy += py_ * s * 2.0
                        dodge += 1
            if dodge:
                urgent = True

        # ---- stuck handling (not while climbing) ----
        speed = math.hypot(me["vel"][0], me["vel"][1])
        if self.last_cmd > 0.5 and speed < 1.5 and me["grounded"] and not climbing:
            self.stuck += 1
        else:
            self.stuck = max(0, self.stuck - 1)
        if self.stuck > 8 and t > self.escape_until:
            self.escape_until = t + 1.5
            self.escape_dir = self.rng.choice((-1, 1))
            self.stuck = 0
        jump = False
        escaping = t < self.escape_until and (vx or vy) and not climbing
        if escaping:
            ang = math.atan2(vy, vx) + self.escape_dir * math.pi / 2
            n = math.hypot(vx, vy)
            vx, vy = math.cos(ang) * n, math.sin(ang) * n
            jump = True
            urgent = True

        # ---- final movement ----
        if climbing:
            ux, uy = unit(gx - x, gy - y)
            mv, frac = [ux, uy], 1.0
            jump = True
        else:
            n = math.hypot(vx, vy)
            if n < 0.05:
                mv, frac = [0.0, 0.0], 0.0
            else:
                frac = 1.0 if (urgent or dist_goal > 30) else 0.5
                if self.on_roof:
                    frac = 0.4
                mv = [vx / n * frac, vy / n * frac]
        self.last_cmd = frac
        act["move"] = mv
        act["jump"] = jump
        act["boost"] = bool((climbing and me["energy"] > 3) or
                            (frac and ((not me["in_zone"] and me["energy"] > 15)
                                       or (retreat and me["energy"] > 50)
                                       or (boost_extra and me["energy"] > 25)
                                       or (escaping and me["energy"] > 10))))

        # ---- aim & fire ----
        fire = [False, False]
        yaw_now, pitch_now = me["aim"]
        aim = None
        if tgt:
            tp = tgt["pos"]
            tv = tgt.get("vel", [0, 0, 0])
            tc = [tp[0], tp[1], tp[2] + fth]
            slots = {w["kind"]: i for i, w in enumerate(me["weapons"]) if w}
            gunw = me["weapons"][slots["gun"]] if "gun" in slots else None
            use_mortar = ftgt is None and "mortar" in slots and (
                (70 < tdist < 220) or ("gun" not in slots and 22 < tdist < 220))
            if allin:
                use_mortar = use_mortar and tdist > 20
            if use_mortar:
                w = me["weapons"][slots["mortar"]]
                sol = mortar_pitch([x, y, z + stats.get("height", 9) + 0.5], tc, tv, w["stats"].get("speed", 72), self.g)
                if sol:
                    aim = [sol[0], max(stats.get("min_pitch", -0.7), min(stats.get("max_pitch", 1.3), sol[1]))]
                    if abs(wrap(aim[0] - yaw_now)) < 0.08 and abs(aim[1] - pitch_now) < 0.08 and w["cooldown"] <= 0.1:
                        fire[slots["mortar"]] = True
            if aim is None:
                spd = gunw["stats"].get("speed", 252) if gunw else 252
                px, py, pz = lead([x, y, mz], tc, tv, spd)
                hd = math.hypot(px - x, py - y)
                aim = [math.atan2(py - y, px - x), math.atan2(pz - mz, max(hd, 0.1))]
                tol = max(0.06, min(0.3, 1.6 * math.atan2(2.5, max(tdist, 1.0))))
                if allin:
                    tol *= 1.6
                aligned = abs(wrap(aim[0] - yaw_now)) < tol and abs(aim[1] - pitch_now) < tol * 1.5
                ydir = math.atan2(tc[1] - y, tc[0] - x)
                for kind, i in slots.items():
                    w = me["weapons"][i]
                    st = w["stats"]
                    if kind == "gun":
                        if aligned and tdist < min(st.get("range", 230), 190) and w.get("ammo", 1) > 0:
                            fire[i] = True
                        elif w.get("ammo", 1) == 0 and not w.get("reload"):
                            act["reload"] = True
                    elif kind == "lightning":
                        cone = math.radians(st.get("cone_deg", 30)) / 2 * 0.8
                        if tdist < st.get("range", 45) * 0.95 and abs(wrap(ydir - yaw_now)) < cone and me["energy"] > 8:
                            fire[i] = True
                    elif kind == "laser":
                        ch = w.get("charge", 0)
                        full = st.get("charge", 0.75)
                        if aligned and tdist < st.get("range", 280) * 0.9 and w["cooldown"] <= 0.2:
                            fire[i] = ch < full * 0.9
                        elif ch > 0:
                            fire[i] = False
        else:
            hear = [s for s in obs["sounds"] if s["kind"] in LOUD and s["dist"] != "far"]
            if recently_hit and self.hit_bearing is not None:
                aim = [self.hit_bearing, 0.0]
            elif hear:
                aim = [hear[0]["bearing"], 0.0]
            else:
                self.scan = wrap(yaw_now + 0.9)
                aim = [self.scan, 0.0]
            w0 = me["weapons"][0]
            if w0 and w0["kind"] == "gun" and w0.get("ammo", 99) < w0["stats"].get("mag", 12) * 0.5 and not w0.get("reload"):
                act["reload"] = True
        act["aim"] = aim
        act["fire"] = fire

        mf.draw.label(f"hp {hp:.0f} {'ROOF' if self.on_roof else 'climb' if climbing else ''} {'R' if retreat else ''} e{len(near)}")
        if goal_roof is not None:
            mf.draw.point([goal_roof["x"], goal_roof["y"], goal_roof["top"]], "#40ff80", 8)
        return act


mf.run(Bot)
