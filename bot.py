"""Survival-first Mech Royale bot.

Placement is what scores, so the plan is: stay inside the safe circle, move quietly (slow walk is silent),
keep the torso facing any threat (rear armor is x1.5), dodge incoming shots, and shoot whatever finds us.
"""
import math
import random
import mechfight as mf

PICK_ORDER = ["armor", "plating", "nanites", "servos", "gun_damage", "gun_rate", "gun_mag", "laser_damage",
              "laser_charge", "lightning_efficiency", "mortar_reload", "sensors", "gyros", "reactor",
              "capacitor", "jets"]
WEAPON_PREF = {"laser": 3, "lightning": 2}
BOUNDS = (-512.0, 555.0, -333.0, 388.0)  # minX, maxX, minY, maxY of the map
LOUD = ("gun", "laser", "laser_charge", "lightning", "mortar", "explosion")


def wrap(a):
    return (a + math.pi) % (2 * math.pi) - math.pi


def unit(x, y):
    n = math.hypot(x, y)
    return (x / n, y / n) if n > 1e-6 else (0.0, 0.0)


def lead(src, tpos, tvel, speed, iters=3):
    """Predicted intercept point for a straight-flying projectile."""
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


class Bot:
    name = "survivor-py"

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

    # ---- zone helpers ----
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
        return (len(a.get("move", [0, 0])) == 2 and all(f(v) for v in a.get("move", [0, 0]))
                and len(a.get("aim", [0, 0])) == 2 and all(f(v) for v in a.get("aim", [0, 0]))
                and len(a.get("fire", [False, False])) == 2)

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

    def _act(self, obs):
        me = obs["self"]
        t = obs["t"]
        x, y, z = me["pos"]
        stats = me["stats"]
        walk = stats.get("walk_speed", 11.7)
        mz = z + stats.get("muzzle_height", 7)
        cz = z + stats.get("height", 9) * 0.5
        act = {"tick": obs["tick"]}

        # ---- upgrades ----
        if obs["offer"]:
            ids = [c["id"] for c in obs["offer"]["choices"]]
            best = min(range(len(ids)), key=lambda i: PICK_ORDER.index(ids[i]) if ids[i] in PICK_ORDER else 99)
            act["pick"] = best

        # ---- events ----
        for e in obs["events"]:
            if e["type"] == "damaged" and self.nlog < 60:
                self.nlog += 1
                mf.log(f"t={t:.1f} hp={me['hp']:.0f} hit {e['amount']:.0f} by {e.get('by')} {e['weapon']} rear={e.get('rear')} alive={obs['alive']} pos=({x:.0f},{y:.0f})")
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
        tgt = enemies[0][1] if enemies and enemies[0][0] < 180 else None
        tdist = enemies[0][0] if tgt else 1e9

        # ---- v4: farm crates / drones for weapons and cubes when no mech is around ----
        zc0, zr0 = self.eff_circle(obs["zone"], t)
        late = t >= 150 or zr0 <= 140          # late game: survive, no detours
        mid = t >= 70
        flim = 0 if late else (60 if mid else 130)       # how far we will go for a crate/drone
        lootlim = 25 if late else (70 if mid else 120)   # how far we will go for a weapon
        cubelim = 12 if late else 30
        ftgt, fth = None, 4.5
        if flim > 0 and tgt is None and not [e for e in enemies if e[0] < 120] and me["in_zone"]:
            best = 1e9
            cands = [(c, 1.0, 1.5) for c in obs["crates"]] + \
                    [(m, 1.6, 1.8) for m in obs["mechs"] if m["kind"] == "npc"]
            for c, pen, h in cands:
                cid = c["id"]
                if self.black.get(cid, -1) > t:
                    continue
                d = math.dist(c["pos"][:2], (x, y))
                if d > flim or math.dist(c["pos"][:2], zc0) > zr0 * 0.85:
                    continue
                if d * pen < best:
                    best, ftgt, fth = d * pen, c, h
            if ftgt is not None:
                if self.farm_id != ftgt["id"]:
                    self.farm_id, self.farm_t0 = ftgt["id"], t
                elif t - self.farm_t0 > 14:  # can't kill it (wall in the way?): give up for a while
                    self.black[ftgt["id"]] = t + 40
                    ftgt = None
        if ftgt is not None:
            tgt = {"pos": ftgt["pos"], "vel": ftgt.get("vel", [0, 0, 0]), "kind": "farm"}
            tdist = math.dist(ftgt["pos"][:2], (x, y))

        # ---- retreat decision ----
        retreat = False
        if near:
            m0 = near[0][1]
            ehp = m0.get("hp", me["hp"])
            retreat = len(near) >= 3 or me["hp"] < 0.35 * me["max_hp"] or me["hp"] < 0.5 * ehp

        # ---- goal: a spot inside the next circle ----
        zn = obs["zone"]
        cc, cr = self.eff_circle(zn, t)
        # v3: hold the rim of the safe circle (12 s ahead), on a per-slot bearing, away from the crowd
        fc, fr = self.eff_circle(zn, t + 12.0)
        rr = max(fr * 0.72, 6.0)
        gx = fc[0] + math.cos(self.ang) * rr
        gy = fc[1] + math.sin(self.ang) * rr
        gx = min(max(gx, BOUNDS[0] + 40), BOUNDS[1] - 40)
        gy = min(max(gy, BOUNDS[2] + 40), BOUNDS[3] - 40)
        ex, ey = gx - fc[0], gy - fc[1]
        ed = math.hypot(ex, ey)
        if ed > rr:  # clamping to the map pushed it out of the circle: pull it back in
            gx, gy = fc[0] + ex / ed * rr, fc[1] + ey / ed * rr
        if math.hypot(gx - x, gy - y) < 25:
            gx, gy = x, y  # close enough: hold still
        dist_goal = math.hypot(gx - x, gy - y)
        # also stay inside the circle as it is right now
        cd = math.hypot(x - cc[0], y - cc[1])
        out_soon = cd > cr * 0.92
        urgent = (not me["in_zone"]) or out_soon or dist_goal / walk * 1.6 > (zn["shrink_end"] - t)
        vx = vy = 0.0
        if dist_goal > 4:
            ux, uy = unit(gx - x, gy - y)
            vx += ux
            vy += uy
        if ftgt is not None:
            vx = vy = 0.0
            if tdist > 32:
                ux, uy = unit(ftgt["pos"][0] - x, ftgt["pos"][1] - y)
                vx, vy = ux, uy

        # ---- loot / cubes when quiet ----
        quiet = not near
        calm = not [e for e in near if e[0] < 60]
        want = None
        if calm and me["weapons"][1] is None:
            best = -1
            for p in obs["pickups"]:
                if p["kind"] not in WEAPON_PREF:
                    continue
                pd = math.dist(p["pos"][:2], (cc[0], cc[1]))
                if pd > cr * 0.85:
                    continue
                dist = math.dist(p["pos"][:2], (x, y))
                score = WEAPON_PREF.get(p["kind"], 0) * 30 - dist
                if dist < lootlim and score > best:
                    best, want = score, p
        if want:
            if math.dist(want["pos"][:2], (x, y)) < self.pickup_r - 0.3 and abs(want["pos"][2] - z) < 3.5:
                act["take"] = want["id"]
            ux, uy = unit(want["pos"][0] - x, want["pos"][1] - y)
            vx, vy = vx * 0.3 + ux * 1.5, vy * 0.3 + uy * 1.5
            urgent = urgent or True
        elif quiet:
            for c in obs["cubes"]:
                if math.dist(c["pos"][:2], (x, y)) < cubelim and math.dist(c["pos"][:2], cc) < cr * 0.85:
                    ux, uy = unit(c["pos"][0] - x, c["pos"][1] - y)
                    vx += ux * 0.9
                    vy += uy * 0.9
                    break

        # ---- enemy avoidance & strafing ----
        if near:
            if t > self.strafe_until:
                self.strafe = self.rng.choice((-1, 1))
                self.strafe_until = t + self.rng.uniform(0.8, 1.8)
            d0, m0 = near[0]
            have_l = any(w and w["kind"] == "lightning" for w in me["weapons"])
            lo, hi = (16, 34) if (have_l and not retreat) else (30, 55)
            ax, ay = unit(x - m0["pos"][0], y - m0["pos"][1])
            if retreat:
                vx += ax * 1.6
                vy += ay * 1.6
            elif d0 < lo:
                vx += ax * 0.5
                vy += ay * 0.5
            elif d0 > hi:
                vx -= ax * 0.8
                vy -= ay * 0.8
            vx += -ay * self.strafe * 0.9
            vy += ax * self.strafe * 0.9
            for d, m in near[1:]:
                bx, by = unit(x - m["pos"][0], y - m["pos"][1])
                vx += bx * 0.5
                vy += by * 0.5
            if retreat:
                urgent = True

        # ---- dodge projectiles & laser telegraphs ----
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
            if math.sqrt(cx_ * cx_ + cy_ * cy_ + cz_ * cz_) > 7:
                continue
            px_, py_ = unit(-v[1], v[0])
            if px_ * cx_ + py_ * cy_ > 0:
                px_, py_ = -px_, -py_
            vx += px_ * 2.0
            vy += py_ * 2.0
            dodge += 1
        for d, m in enemies:
            if d < 200 and m.get("charging"):
                ang = math.atan2(y - m["pos"][1], x - m["pos"][0])
                if abs(wrap(ang - m["aim"][0])) < 0.35:
                    px_, py_ = unit(-math.sin(m["aim"][0]), math.cos(m["aim"][0]))
                    s = 1 if self.strafe > 0 else -1
                    vx += px_ * s * 2.0
                    vy += py_ * s * 2.0
                    dodge += 1
        if dodge:
            urgent = True

        # ---- stuck handling ----
        speed = math.hypot(me["vel"][0], me["vel"][1])
        if self.last_cmd > 0.5 and speed < 1.5 and me["grounded"]:
            self.stuck += 1
        else:
            self.stuck = max(0, self.stuck - 1)
        if self.stuck > 8 and t > self.escape_until:
            self.escape_until = t + 1.5
            self.escape_dir = self.rng.choice((-1, 1))
            self.stuck = 0
        jump = False
        if t < self.escape_until and (vx or vy):
            ang = math.atan2(vy, vx) + self.escape_dir * math.pi / 2
            n = math.hypot(vx, vy)
            vx, vy = math.cos(ang) * n, math.sin(ang) * n
            jump = True
            urgent = True

        # ---- final movement ----
        n = math.hypot(vx, vy)
        if n < 0.05:
            mv, frac = [0.0, 0.0], 0.0
        else:
            ux, uy = vx / n, vy / n
            frac = 1.0 if (urgent or dist_goal > 30) else 0.5
            mv = [ux * frac, uy * frac]
        self.last_cmd = frac
        act["move"] = mv
        act["jump"] = jump
        act["boost"] = bool(frac and ((not me["in_zone"] and me["energy"] > 15)
                                      or (retreat and me["energy"] > 50)
                                      or (t < self.escape_until and me["energy"] > 10)))

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
            use_mortar = ftgt is None and (("mortar" in slots and 70 < tdist < 220)
                                           or ("mortar" in slots and "gun" not in slots and 22 < tdist < 220))
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
                aligned = abs(wrap(aim[0] - yaw_now)) < tol and abs(aim[1] - pitch_now) < tol * 1.5
                ydir = math.atan2(tc[1] - y, tc[0] - x)
                for kind, i in slots.items():
                    w = me["weapons"][i]
                    st = w["stats"]
                    if kind == "gun":
                        if aligned and tdist < min(st.get("range", 230), 170) and w.get("ammo", 1) > 0:
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
            if hear:
                aim = [hear[0]["bearing"], 0.0]
            elif self.hit_bearing is not None and t - self.hit_t < 2.0:
                aim = [self.hit_bearing, 0.0]
            else:
                self.scan = wrap(yaw_now + 0.9)
                aim = [self.scan, 0.0]
            w0 = me["weapons"][0]
            if w0 and w0["kind"] == "gun" and w0.get("ammo", 99) < w0["stats"].get("mag", 12) * 0.5 and not w0.get("reload"):
                act["reload"] = True
        act["aim"] = aim
        act["fire"] = fire

        mf.draw.label(f"hp {me['hp']:.0f} {'R' if retreat else ''} e{len(near)}")
        return act


mf.run(Bot)
