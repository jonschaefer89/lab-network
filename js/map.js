// Network map: a canvas renderer with two deterministic layouts.
//
// Lab map: the lab sits at the centre; each ring is one degree of connection;
// each lab member owns an angular sector holding the people reached through
// them. A person's angle comes from the person who connects them to the lab,
// so clusters stay together and every ring keeps even spacing. Only the
// connecting lines are drawn until you point at or select someone.
//
// Person map: one person at the centre, their direct ties around them grouped
// by relationship kind, and their shortest route(s) back to the lab.

import { GROUP_OF, GROUP_PRIORITY, dominantGroup, initials, safeUrl } from "./ui.js";

const TAU = Math.PI * 2;
const START = -Math.PI / 2;
const LABEL_PX = 12;

function readColors() {
  const cs = getComputedStyle(document.documentElement);
  const v = (n) => cs.getPropertyValue(n).trim();
  return {
    paper: v("--paper"), sheet: v("--sheet"), ink: v("--ink"), ink2: v("--ink-2"), ink3: v("--ink-3"),
    contour: v("--contour"), contourLabel: v("--contour-label"), rule: v("--rule-strong"),
    research: v("--research"), supervision: v("--supervision"), association: v("--association"),
  };
}

function polar(a, r) {
  return [Math.cos(a) * r, Math.sin(a) * r];
}

function pairKey(a, b) {
  return a < b ? a + "|" + b : b + "|" + a;
}

function ease(t) {
  return 1 - Math.pow(1 - t, 3);
}

function reducedMotion() {
  return window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

export class NetworkMap {
  constructor(canvas, { onSelect, onHover, onBackground } = {}) {
    this.canvas = canvas;
    this.ctx = canvas.getContext("2d");
    this.onSelect = onSelect || (() => {});
    this.onHover = onHover || (() => {});
    this.onBackground = onBackground || (() => {});
    this.nodes = [];
    this.links = [];
    this.byId = new Map();
    this.adj = new Map();
    this.mode = "lab";
    this.k = 1;
    this.tx = 0;
    this.ty = 0;
    this.sel = null;
    this.hover = null;
    this.focus = null;
    this.highlight = null;
    this.hlContext = new Set();
    this.hiddenGroups = new Set();
    this.showAllTies = false;
    this.images = new Map();
    this.rings = [];
    this.sectors = [];
    this.anim = null;
    this.colors = readColors();
    this.family = getComputedStyle(document.body).fontFamily;
    this.width = 0;
    this.height = 0;
    this.fitted = false;
    this.userMoved = false;     // true once the person zooms or pans; until then resizes re-fit the map
    this.pendingFit = false;
    this._pending = false;
    this.bind();
    this.resize();
    document.addEventListener("visibilitychange", () => { if (!document.hidden) this.draw(); });
    const mq = window.matchMedia("(prefers-color-scheme: dark)");
    if (mq.addEventListener) mq.addEventListener("change", () => { this.colors = readColors(); this.draw(); });
  }

  // ------------------------------------------------------------------ data

  setLab(data, { keepView = false } = {}) {
    this.mode = "lab";
    this._ingest(data.nodes, data.links);
    this.roots = data.roots || [];
    this.layoutLab();
    this._transition(keepView);
  }

  setEgo(data, { keepView = false } = {}) {
    this.mode = "ego";
    this._ingest(data.nodes, data.links);
    this.center = data.center;
    this.routes = data.routes || [];
    this.layoutEgo();
    this._transition(keepView);
  }

  _ingest(nodes, links) {
    const prev = this.byId;
    this.nodes = nodes.map((n) => {
      const old = prev.get(n.id);
      return { ...n, cx: old ? old.cx : undefined, cy: old ? old.cy : undefined };
    });
    this.byId = new Map(this.nodes.map((n) => [n.id, n]));
    this.links = links.filter((l) => this.byId.has(l.a) && this.byId.has(l.b)).map((l) => ({
      ...l, key: pairKey(l.a, l.b), group: dominantGroup(l.types),
    }));
    this.linkByKey = new Map(this.links.map((l) => [l.key, l]));
    this.adj = new Map();
    for (const l of this.links) {
      if (!this.adj.has(l.a)) this.adj.set(l.a, new Set());
      if (!this.adj.has(l.b)) this.adj.set(l.b, new Set());
      this.adj.get(l.a).add(l.b);
      this.adj.get(l.b).add(l.a);
    }
  }

  // ------------------------------------------------------------------ layouts

  layoutLab() {
    const nodes = this.nodes;
    const children = new Map();
    const orphans = [];
    for (const n of nodes) {
      if (n.parent && this.byId.has(n.parent)) {
        if (!children.has(n.parent)) children.set(n.parent, []);
        children.get(n.parent).push(n);
      } else if (!this.roots.includes(n.id)) {
        orphans.push(n);
      }
    }
    const linkGroup = (a, b) => {
      const l = this.linkByKey.get(pairKey(a, b));
      return l ? l.group : "association";
    };
    for (const [pid, list] of children) {
      list.sort((x, y) => {
        const gx = GROUP_PRIORITY.indexOf(linkGroup(pid, x.id)), gy = GROUP_PRIORITY.indexOf(linkGroup(pid, y.id));
        return gx - gy || x.name.localeCompare(y.name);
      });
    }
    const maxDepth = Math.max(1, ...nodes.map((n) => (n.degree ?? 0)));
    // Base ring radii; scaled up below until every ring has room.
    const base = [0, 190];
    for (let d = 2; d <= maxDepth + 1; d++) base[d] = base[d - 1] + (d === 2 ? 230 : 210);
    const spacing = 15;
    const need = (n, depth, R) => {
      const own = spacing / R[Math.min(depth, R.length - 1)];
      const ch = children.get(n.id) || [];
      if (!ch.length) return own;
      let s = 0;
      for (const c of ch) s += need(c, depth + 1, R);
      return Math.max(own, s);
    };
    const roots = this.roots.map((id) => this.byId.get(id)).filter(Boolean);
    const orphanDepth = maxDepth + 1;
    const total = (R) => {
      let w = 0;
      for (const r of roots) {
        const ch = children.get(r.id) || [];
        let s = 0;
        for (const c of ch) s += need(c, 1, R);
        w += Math.max(s, 0.12);
      }
      if (orphans.length) w += orphans.length * spacing / R[Math.min(orphanDepth, R.length - 1)];
      return w;
    };
    const gapPerRoot = 0.05;
    let W = total(base) + gapPerRoot * (roots.length + (orphans.length ? 1 : 0));
    const f = Math.max(1, W / TAU);
    const R = base.map((r) => r * f);
    W = total(R);
    const gaps = gapPerRoot * (roots.length + (orphans.length ? 1 : 0));
    const scale = (TAU - gaps) / W;
    const labR = roots.length > 1 ? 150 : 0;

    this.rings = [];
    for (let d = 1; d <= maxDepth; d++) {
      if (nodes.some((n) => n.degree === d)) this.rings.push({ r: R[d], label: ordinal(d) + " degree" });
    }
    this.sectors = [];
    let a = START;
    const place = (n, depth, a0, a1) => {
      const mid = (a0 + a1) / 2;
      const r = R[Math.min(depth, R.length - 1)];
      [n.x, n.y] = polar(mid, r);
      n.a = mid;
      n.depth = depth;
      n.R = r;
      n.span = (a1 - a0);
      n.r = n.ghost ? 4.5 : depth === 1 ? 8.5 : depth === 2 ? 6.5 : 5.5;
      const ch = children.get(n.id) || [];
      if (!ch.length) return;
      const needs = ch.map((c) => need(c, depth + 1, R) * scale);
      const sum = needs.reduce((s, x) => s + x, 0);
      let cur = mid - sum / 2;
      ch.forEach((c, i) => {
        place(c, depth + 1, cur, cur + needs[i]);
        cur += needs[i];
      });
    };
    for (const root of roots) {
      const ch = children.get(root.id) || [];
      let s = 0;
      for (const c of ch) s += need(c, 1, R);
      const span = Math.max(s, 0.12) * scale;
      const a0 = a, a1 = a + span;
      const mid = (a0 + a1) / 2;
      root.depth = 0;
      root.a = mid;
      [root.x, root.y] = polar(mid, labR);
      root.r = 17;
      root.span = span;
      root.R = labR;
      let cur = mid - (s * scale) / 2;
      for (const c of ch) {
        const w = need(c, 1, R) * scale;
        place(c, 1, cur, cur + w);
        cur += w;
      }
      this.sectors.push({ id: root.id, a0, a1, name: root.name, count: countDesc(root.id, children) });
      a = a1 + gapPerRoot;
    }
    // Lab members sit on a small circle near their sectors, spread far enough apart to read.
    const spread = spreadAngles(roots.map((r) => r.a), TAU / Math.max(roots.length + 1, 4));
    roots.forEach((r, i) => { r.a = spread[i]; [r.x, r.y] = polar(spread[i], labR); });
    let minGap = Infinity;
    for (let i = 0; i < roots.length && roots.length > 1; i++) {
      const a1 = roots[i], a2 = roots[(i + 1) % roots.length];
      minGap = Math.min(minGap, Math.hypot(a1.x - a2.x, a1.y - a2.y));
    }
    this.labGap = Number.isFinite(minGap) ? minGap : Infinity;
    if (orphans.length) {
      const r = R[Math.min(orphanDepth, R.length - 1)];
      const w = orphans.length * (spacing / r) * scale;
      orphans.sort((x, y) => x.name.localeCompare(y.name));
      orphans.forEach((n, i) => {
        const ang = a + (i + 0.5) * (w / orphans.length);
        [n.x, n.y] = polar(ang, r);
        n.a = ang;
        n.depth = orphanDepth;
        n.R = r;
        n.span = w / orphans.length;
        n.r = 5;
      });
      this.rings.push({ r, label: "No recorded path" });
      this.sectors.push({ id: null, a0: a, a1: a + w, name: "No recorded path", count: orphans.length });
    }
    this.outerR = Math.max(labR, ...this.rings.map((x) => x.r));
    const outer = nodes.filter((n) => Math.abs((n.R || 0) - this.outerR) < 1 && n.span);
    // Typical gap between neighbours on the outer ring, in world units: decides whether their names fit.
    this.outerSpacing = outer.length ? outer.map((n) => n.span * n.R).sort((a, b) => a - b)[Math.floor(outer.length / 2)] : 0;
  }

  layoutEgo() {
    const c = this.byId.get(this.center);
    if (!c) return;
    c.x = 0; c.y = 0; c.a = 0; c.r = 22; c.depth = 0; c.R = 0; c.span = TAU;
    const nbrs = this.nodes.filter((n) => n.role_in_view === "neighbor");
    for (const n of this.nodes) n.outside = n.id !== this.center && n.shown === false;
    const groupOf = (n) => {
      const l = this.linkByKey.get(pairKey(c.id, n.id));
      return l ? l.group : "association";
    };
    const groups = { supervision: [], research: [], association: [] };
    for (const n of nbrs) groups[groupOf(n)].push(n);
    for (const g of Object.values(groups)) {
      g.sort((x, y) => (x.degree ?? 99) - (y.degree ?? 99) || x.name.localeCompare(y.name));
    }
    const N = nbrs.length;
    const Re = Math.max(170, (N * 24) / TAU);
    const used = GROUP_PRIORITY.filter((g) => groups[g].length);
    const gap = used.length > 1 ? 0.22 : 0;
    const avail = TAU - gap * used.length;
    // Supervision centred at the top, research clockwise from it, association after.
    const spans = used.map((g) => Math.max(0.35, (groups[g].length / Math.max(N, 1)) * avail));
    const scale = avail / (spans.reduce((s, x) => s + x, 0) || 1);
    let a = START - (spans[0] * scale) / 2;
    this.sectors = [];
    used.forEach((g, gi) => {
      const span = spans[gi] * scale;
      const list = groups[g];
      list.forEach((n, i) => {
        const ang = a + (i + 0.5) * (span / list.length);
        [n.x, n.y] = polar(ang, Re);
        n.a = ang; n.R = Re; n.depth = 1; n.span = span / list.length; n.r = 11;
      });
      this.sectors.push({ id: g, a0: a, a1: a + span, name: groupName(g), count: list.length, group: g });
      a += span + gap;
    });
    // Route stops beyond the ring: placed outward from the neighbour they lead through.
    const placed = new Set([c.id, ...nbrs.map((n) => n.id)]);
    const outer = new Map();
    for (const p of this.routes) {
      // p runs lab -> ... -> centre
      for (let i = p.length - 3; i >= 0; i--) {
        const id = p[i];
        if (placed.has(id)) continue;
        const bridge = this.byId.get(p[p.length - 2]);
        const steps = p.length - 2 - i;
        if (!outer.has(id)) outer.set(id, { angs: [], steps });
        outer.get(id).angs.push(bridge ? bridge.a : 0);
      }
    }
    for (const [id, info] of outer) {
      const n = this.byId.get(id);
      if (!n) continue;
      const ang = Math.atan2(info.angs.reduce((s, x) => s + Math.sin(x), 0), info.angs.reduce((s, x) => s + Math.cos(x), 0));
      const r = Re + 150 * info.steps;
      [n.x, n.y] = polar(ang, r);
      n.a = ang; n.R = r; n.depth = 1 + info.steps; n.span = 0.3; n.r = n.isLab ? 14 : 8;
    }
    for (const n of this.nodes) {
      if (n.x === undefined) { n.x = 0; n.y = 0; n.r = 6; n.a = 0; n.R = 0; n.span = 0.1; }
    }
    this.rings = [{ r: Re, label: "Direct ties" }];
    this.outerR = Math.max(Re, ...this.nodes.map((n) => n.R || 0));
  }

  _transition(keepView) {
    const animate = !reducedMotion() && this.nodes.some((n) => n.cx !== undefined);
    for (const n of this.nodes) {
      if (n.cx === undefined) {
        const p = n.parent && this.byId.get(n.parent);
        n.cx = p && p.cx !== undefined ? p.cx : n.x * 0.6;
        n.cy = p && p.cy !== undefined ? p.cy : n.y * 0.6;
      }
      n.sx = n.cx; n.sy = n.cy;
    }
    if (!keepView || !this.fitted) this.fit(animate);
    if (animate) {
      this.anim = { t0: performance.now(), dur: 520 };
    } else {
      for (const n of this.nodes) { n.cx = n.x; n.cy = n.y; }
      this.anim = null;
    }
    this.requestDraw();
  }

  // ------------------------------------------------------------------ selection

  setSelection(id, routes = []) {
    this.sel = id && this.byId.has(id) ? id : null;
    if (!this.sel) {
      this.focus = null;
    } else {
      const nodes = new Set([this.sel]);
      const links = new Set();
      for (const nb of this.adj.get(this.sel) || []) {
        nodes.add(nb);
        links.add(pairKey(this.sel, nb));
      }
      const routeLinks = new Set();
      for (const p of routes) {
        for (let i = 0; i < p.length; i++) {
          if (this.byId.has(p[i])) nodes.add(p[i]);
          if (i > 0) routeLinks.add(pairKey(p[i - 1], p[i]));
        }
      }
      this.focus = { nodes, links, routeLinks };
    }
    this.requestDraw();
  }

  setHighlight(ids) {
    this.highlight = ids && ids.size ? ids : null;
    // On the lab map, the people between each match and the lab stay visible so the route reads.
    this.hlContext = new Set();
    if (this.highlight && this.mode === "lab") {
      for (const id of this.highlight) {
        let n = this.byId.get(id);
        const seen = new Set();
        while (n && n.parent && !seen.has(n.id)) {
          seen.add(n.id);
          if (!this.highlight.has(n.parent)) this.hlContext.add(n.parent);
          n = this.byId.get(n.parent);
        }
      }
    }
    this.requestDraw();
  }

  inHighlight(id) {
    const n = this.byId.get(id);
    return this.highlight.has(id) || this.hlContext.has(id) || (n && n.depth === 0 && this.mode === "lab");
  }

  setHiddenGroups(set) {
    this.hiddenGroups = set;
    this.requestDraw();
  }

  centerOn(id, zoom) {
    const n = this.byId.get(id);
    if (!n) return;
    this.userMoved = true;
    const k = zoom || Math.max(this.k, 1.1);
    this._animateView(k, this.width / 2 - n.x * k, this.height / 2 - n.y * k);
  }

  // ------------------------------------------------------------------ view transform

  fit(animate = true) {
    if (this.width < 40 || this.height < 40) {
      // The canvas has no real size yet (pane still opening); fit as soon as it does.
      this.pendingFit = true;
      return;
    }
    this.pendingFit = false;
    this.userMoved = false;
    const pad = 36;
    const half = Math.min(this.width, this.height) / 2 - pad;
    let k;
    if (this.mode === "lab") {
      // First try to fit with outer names shown (rim pushed out by their length); otherwise fit without them.
      // The overview shows structure; outer names appear when you zoom in (see outerLabels).
      k = half / ((this.outerR || 200) + 36 + 52);
    } else {
      k = half / (this.rimRadius() + 40);
    }
    const kk = Math.max(0.05, Math.min(k, 2.2));
    this.fitK = kk;
    const tx = this.width / 2, ty = this.height / 2;
    this.fitted = true;
    if (animate && this.width) this._animateView(kk, tx, ty);
    else { this.k = kk; this.tx = tx; this.ty = ty; this.requestDraw(); }
  }

  zoomBy(factor, cx = this.width / 2, cy = this.height / 2) {
    this.userMoved = true;
    const k = Math.max(0.04, Math.min(8, this.k * factor));
    const f = k / this.k;
    this.tx = cx - (cx - this.tx) * f;
    this.ty = cy - (cy - this.ty) * f;
    this.k = k;
    this.requestDraw();
  }

  _animateView(k, tx, ty) {
    if (reducedMotion()) { this.k = k; this.tx = tx; this.ty = ty; this.requestDraw(); return; }
    this.viewAnim = { t0: performance.now(), dur: 420, from: [this.k, this.tx, this.ty], to: [k, tx, ty] };
    this.requestDraw();
  }

  toWorld(sx, sy) {
    return [(sx - this.tx) / this.k, (sy - this.ty) / this.k];
  }

  resize() {
    const rect = this.canvas.getBoundingClientRect();
    const dpr = window.devicePixelRatio || 1;
    const w = Math.max(1, rect.width), hgt = Math.max(1, rect.height);
    const changed = w !== this.width || hgt !== this.height;
    if (this.width && changed) {
      this.tx += (w - this.width) / 2;
      this.ty += (hgt - this.height) / 2;
    }
    this.width = w;
    this.height = hgt;
    this.canvas.width = Math.round(w * dpr);
    this.canvas.height = Math.round(hgt * dpr);
    this.dpr = dpr;
    if (changed && (this.pendingFit || (this.fitted && !this.userMoved))) this.fit(false);
    this.requestDraw();
  }

  // ------------------------------------------------------------------ input

  bind() {
    const cv = this.canvas;
    const pointers = new Map();
    let drag = null;
    let pinch = null;
    cv.addEventListener("pointerdown", (e) => {
      cv.setPointerCapture(e.pointerId);
      pointers.set(e.pointerId, [e.offsetX, e.offsetY]);
      if (pointers.size === 1) {
        drag = { x: e.offsetX, y: e.offsetY, tx: this.tx, ty: this.ty, moved: false };
      } else if (pointers.size === 2) {
        const [p1, p2] = [...pointers.values()];
        pinch = { d: Math.hypot(p1[0] - p2[0], p1[1] - p2[1]), k: this.k };
        drag = null;
      }
    });
    cv.addEventListener("pointermove", (e) => {
      if (pointers.has(e.pointerId)) pointers.set(e.pointerId, [e.offsetX, e.offsetY]);
      if (pinch && pointers.size === 2) {
        const [p1, p2] = [...pointers.values()];
        const d = Math.hypot(p1[0] - p2[0], p1[1] - p2[1]);
        const target = pinch.k * (d / pinch.d);
        this.zoomBy(target / this.k, (p1[0] + p2[0]) / 2, (p1[1] + p2[1]) / 2);
        return;
      }
      if (drag) {
        const dx = e.offsetX - drag.x, dy = e.offsetY - drag.y;
        if (!drag.moved && Math.hypot(dx, dy) > 4) {
          drag.moved = true;
          cv.classList.add("dragging");
          this.onHover(null);
        }
        if (drag.moved) {
          this.userMoved = true;
          this.tx = drag.tx + dx;
          this.ty = drag.ty + dy;
          this.viewAnim = null;
          this.requestDraw();
          return;
        }
      }
      const hit = this.hitTest(e.offsetX, e.offsetY);
      const id = hit ? hit.id : null;
      if (id !== this.hover) {
        this.hover = id;
        cv.classList.toggle("pointing", !!hit);
        this.requestDraw();
      }
      this.onHover(hit, e);
    });
    const end = (e) => {
      pointers.delete(e.pointerId);
      if (pointers.size < 2) pinch = null;
      if (drag && !drag.moved && e.type === "pointerup") {
        const hit = this.hitTest(e.offsetX, e.offsetY);
        if (hit) this.onSelect(hit.id);
        else this.onBackground();
      }
      drag = null;
      cv.classList.remove("dragging");
    };
    cv.addEventListener("pointerup", end);
    cv.addEventListener("pointercancel", end);
    cv.addEventListener("pointerleave", () => {
      if (!drag) {
        this.hover = null;
        this.onHover(null);
        this.requestDraw();
      }
    });
    cv.addEventListener("wheel", (e) => {
      e.preventDefault();
      const scale = e.deltaMode === 1 ? 0.05 : 0.0016;
      this.viewAnim = null;
      this.zoomBy(Math.exp(-e.deltaY * scale * (e.ctrlKey ? 4 : 1)), e.offsetX, e.offsetY);
    }, { passive: false });
    cv.addEventListener("dblclick", (e) => {
      this.viewAnim = null;
      this.zoomBy(1.8, e.offsetX, e.offsetY);
    });
    cv.addEventListener("keydown", (e) => {
      const step = 60;
      const actions = {
        "+": () => this.zoomBy(1.25), "=": () => this.zoomBy(1.25), "-": () => this.zoomBy(0.8),
        "0": () => this.fit(), ArrowLeft: () => { this.tx += step; this.userMoved = true; },
        ArrowRight: () => { this.tx -= step; this.userMoved = true; },
        ArrowUp: () => { this.ty += step; this.userMoved = true; }, ArrowDown: () => { this.ty -= step; this.userMoved = true; },
      };
      if (actions[e.key]) {
        e.preventDefault();
        actions[e.key]();
        this.requestDraw();
      }
    });
    if (window.ResizeObserver) new ResizeObserver(() => this.resize()).observe(cv.parentElement);
    else window.addEventListener("resize", () => this.resize());
  }

  hitTest(sx, sy) {
    const [wx, wy] = this.toWorld(sx, sy);
    let best = null, bestD = Infinity;
    for (const n of this.nodes) {
      const dx = n.cx - wx, dy = n.cy - wy;
      const rr = this.rad(n) + 5 / this.k;
      const d = dx * dx + dy * dy;
      if (d <= rr * rr && d < bestD) { best = n; bestD = d; }
    }
    return best;
  }

  /** True when names on the outermost ring are drawn at zoom k, so the rim must sit beyond them. */
  outerLabels(k = this.k) {
    if (this.mode !== "lab" || this.outerSpacing * k < LABEL_PX - 1) return false;
    if (!this.fitK || k > this.fitK * 1.12) return true;          // zoomed in past the overview
    // At the overview zoom, show outer names only if the rim pushed out by them still fits on screen.
    return ((this.outerR || 0) + 150 / k + 30) * k <= Math.min(this.width, this.height) / 2;
  }

  rimRadius(k = this.k) {
    if (this.mode !== "lab") return (this.outerR || 0) + 165;
    return (this.outerR || 0) + (this.outerLabels(k) ? 150 / k : 34);
  }

  /** Drawn radius in world units, with a minimum on-screen size so nothing vanishes when zoomed out. */
  rad(n) {
    const k = this.k;
    if (this.mode === "lab" && n.depth === 0) {
      const room = (this.labGap || Infinity) * k / 2 - 1.5;   // half the on-screen gap to the next lab member
      return Math.max(n.r, Math.max(6, Math.min(13, room)) / k);
    }
    if (this.mode === "ego" && n.id === this.center) return Math.max(n.r, 18 / k);
    if (n.id === this.sel) return Math.max(n.r, 7 / k);
    return Math.max(n.r, (n.depth <= 1 ? 3.2 : 2.4) / k);
  }

  isFaded(n) {
    if (this.highlight) return !this.inHighlight(n.id) && !(this.focus && this.focus.nodes.has(n.id));
    if (this.focus) return !this.focus.nodes.has(n.id);
    return false;
  }

  // ------------------------------------------------------------------ drawing

  requestDraw() {
    if (this._pending) return;
    this._pending = true;
    // Animation frames pause in hidden or background pages; the timer guarantees a redraw anyway.
    const run = () => {
      if (!this._pending) return;
      this._pending = false;
      cancelAnimationFrame(this._raf);
      clearTimeout(this._timer);
      this.draw();
    };
    this._raf = requestAnimationFrame(run);
    this._timer = setTimeout(run, 50);
  }

  photo(n) {
    const url = safeUrl(n.photoUrl);
    if (!url) return null;
    let rec = this.images.get(url);
    if (!rec) {
      const img = new Image();
      img.referrerPolicy = "no-referrer";
      img.decoding = "async";
      rec = { img, ok: false, failed: false };
      img.onload = () => { rec.ok = true; this.requestDraw(); };
      img.onerror = () => { rec.failed = true; };
      img.src = url;
      this.images.set(url, rec);
    }
    return rec.ok ? rec.img : null;
  }

  draw() {
    const now = performance.now();
    let more = false;
    if (this.anim) {
      const t = Math.min(1, (now - this.anim.t0) / this.anim.dur);
      const e = ease(t);
      for (const n of this.nodes) {
        n.cx = n.sx + (n.x - n.sx) * e;
        n.cy = n.sy + (n.y - n.sy) * e;
      }
      if (t >= 1) this.anim = null; else more = true;
    }
    if (this.viewAnim) {
      const t = Math.min(1, (now - this.viewAnim.t0) / this.viewAnim.dur);
      const e = ease(t);
      const [k0, x0, y0] = this.viewAnim.from, [k1, x1, y1] = this.viewAnim.to;
      this.k = k0 + (k1 - k0) * e;
      this.tx = x0 + (x1 - x0) * e;
      this.ty = y0 + (y1 - y0) * e;
      if (t >= 1) this.viewAnim = null; else more = true;
    }
    const ctx = this.ctx, C = this.colors, k = this.k, dpr = this.dpr;
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.fillStyle = C.paper;
    ctx.fillRect(0, 0, this.canvas.width, this.canvas.height);
    ctx.setTransform(k * dpr, 0, 0, k * dpr, this.tx * dpr, this.ty * dpr);

    this.drawContours(ctx, C, k);
    const focus = this.focus;
    const hoverId = !focus ? this.hover : null;
    const linkVisible = (l) => !l.types.every((t) => this.hiddenGroups.has(GROUP_OF[t]));
    // Base lines.
    for (const l of this.links) {
      if (!linkVisible(l)) continue;
      const emphasised = (focus && (focus.links.has(l.key) || focus.routeLinks.has(l.key))) ||
        (hoverId && (l.a === hoverId || l.b === hoverId));
      if (emphasised) continue;
      const base = this.mode === "ego" ? (l.spoke || l.route) : l.tree;
      if (!base && !this.showAllTies) continue;
      const faded = focus || (this.highlight && !(this.inHighlight(l.a) && this.inHighlight(l.b)));
      this.strokeLink(ctx, l, { alpha: faded ? 0.1 : base ? 0.42 : 0.18, width: base ? 1.15 : 0.8 });
    }
    // Emphasised lines on top.
    for (const l of this.links) {
      if (!linkVisible(l)) continue;
      const route = focus && focus.routeLinks.has(l.key);
      const near = (focus && focus.links.has(l.key)) || (hoverId && (l.a === hoverId || l.b === hoverId));
      if (!route && !near) continue;
      this.strokeLink(ctx, l, { alpha: route ? 1 : 0.8, width: route ? 3.4 : 1.8, casing: route });
    }
    // Nodes.
    const ordered = [...this.nodes].sort((a, b) => this.drawRank(a) - this.drawRank(b));
    for (const n of ordered) this.drawNode(ctx, C, n, k);
    // Labels in screen space.
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    this.drawSectorLabels(ctx, C, k);
    this.drawLabels(ctx, C, k);
    if (more || (this.anim || this.viewAnim)) this.requestDraw();
  }

  drawRank(n) {
    if (n.id === this.sel) return 5;
    if (n.id === this.hover) return 4;
    if (this.focus && this.focus.nodes.has(n.id)) return 3;
    if (this.highlight && this.highlight.has(n.id)) return 3;
    return n.depth === 0 ? 2 : 1;
  }

  drawContours(ctx, C, k) {
    ctx.save();
    ctx.lineWidth = 1 / k;
    ctx.strokeStyle = C.contour;
    ctx.setLineDash([4 / k, 5 / k]);
    for (const ring of this.rings) {
      ctx.beginPath();
      ctx.arc(0, 0, ring.r, 0, TAU);
      ctx.stroke();
    }
    ctx.setLineDash([]);
    // Sector arcs just outside the outermost ring, like bearings on a chart rim.
    const rimR = this.rimRadius();
    ctx.lineWidth = 3 / k;
    for (const s of this.sectors) {
      if (s.a1 - s.a0 < 0.002) continue;
      ctx.strokeStyle = s.group ? C[s.group] : C.rule;
      ctx.globalAlpha = s.group ? 0.55 : 1;
      ctx.beginPath();
      ctx.arc(0, 0, rimR, s.a0 + 0.004, s.a1 - 0.004);
      ctx.stroke();
    }
    ctx.restore();
  }

  strokeLink(ctx, l, { alpha, width, casing }) {
    const a = this.byId.get(l.a), b = this.byId.get(l.b);
    if (!a || !b) return;
    const k = this.k, C = this.colors;
    const path = () => {
      ctx.beginPath();
      if (this.mode === "lab" && l.tree) {
        const [p, c] = a.depth <= b.depth ? [a, b] : [b, a];
        const mr = (Math.hypot(p.cx, p.cy) + Math.hypot(c.cx, c.cy)) / 2;
        const pa = Math.atan2(p.cy, p.cx), ca = Math.atan2(c.cy, c.cx);
        ctx.moveTo(p.cx, p.cy);
        ctx.bezierCurveTo(Math.cos(pa) * mr, Math.sin(pa) * mr, Math.cos(ca) * mr, Math.sin(ca) * mr, c.cx, c.cy);
      } else if (this.mode === "ego" && l.spoke) {
        ctx.moveTo(a.cx, a.cy);
        ctx.lineTo(b.cx, b.cy);
      } else {
        const mx = (a.cx + b.cx) / 2, my = (a.cy + b.cy) / 2;
        const pull = this.mode === "lab" ? 0.55 : 0.4;
        ctx.moveTo(a.cx, a.cy);
        ctx.quadraticCurveTo(mx * pull, my * pull, b.cx, b.cy);
      }
    };
    ctx.save();
    ctx.lineCap = "round";
    if (casing) {
      ctx.globalAlpha = 1;
      ctx.strokeStyle = C.sheet;
      ctx.lineWidth = (width + 3.5) / k;
      path();
      ctx.stroke();
    }
    const dash = l.prov.includes("public") ? [] : l.prov.includes("reported") ? [6 / k, 4 / k] : [0.5 / k, 3.5 / k];
    ctx.setLineDash(dash);
    ctx.globalAlpha = alpha;
    ctx.strokeStyle = C[l.group] || C.ink2;
    ctx.lineWidth = width / k;
    path();
    ctx.stroke();
    ctx.restore();
    if (casing || (this.mode === "ego" && l.spoke)) this.drawArrows(ctx, l, a, b, alpha);
  }

  drawArrows(ctx, l, a, b, alpha) {
    // One-way records (mentor, postdoc, committee) get an arrowhead toward their `to` person.
    if (!l.directional || !l.directional.length || this.k < 0.35) return;
    const C = this.colors, k = this.k;
    const seen = new Set();
    for (const d of l.directional) {
      const key = d.type + ":" + d.from + ">" + d.to;
      if (seen.has(key)) continue;
      seen.add(key);
      // Postdoc records run postdoc -> supervisor; flip them so every arrow points from supervisor to supervisee.
      const [fid, tid] = d.type === "postdoc" ? [d.to, d.from] : [d.from, d.to];
      const from = this.byId.get(fid), to = this.byId.get(tid);
      if (!from || !to) continue;
      const ang = Math.atan2(to.cy - from.cy, to.cx - from.cx);
      const tipX = to.cx - Math.cos(ang) * (this.rad(to) + 3 / k), tipY = to.cy - Math.sin(ang) * (this.rad(to) + 3 / k);
      const s = 7 / k;
      ctx.save();
      ctx.globalAlpha = Math.min(1, alpha + 0.2);
      ctx.fillStyle = C.supervision;
      ctx.beginPath();
      ctx.moveTo(tipX, tipY);
      ctx.lineTo(tipX - Math.cos(ang - 0.42) * s, tipY - Math.sin(ang - 0.42) * s);
      ctx.lineTo(tipX - Math.cos(ang + 0.42) * s, tipY - Math.sin(ang + 0.42) * s);
      ctx.closePath();
      ctx.fill();
      ctx.restore();
    }
  }

  drawNode(ctx, C, n, k) {
    const faded = this.isFaded(n);
    const selected = n.id === this.sel;
    const hovered = n.id === this.hover;
    const hl = this.highlight && this.highlight.has(n.id);
    const r = this.rad(n);
    ctx.save();
    ctx.globalAlpha = faded ? 0.22 : 1;
    if (selected || hl) {
      ctx.beginPath();
      ctx.arc(n.cx, n.cy, r + 4 / k, 0, TAU);
      ctx.fillStyle = selected ? C.ink : C.sheet;
      ctx.fill();
      if (hl && !selected) {
        ctx.lineWidth = 2 / k;
        ctx.strokeStyle = C.ink;
        ctx.stroke();
      }
    }
    ctx.beginPath();
    ctx.arc(n.cx, n.cy, r, 0, TAU);
    if (n.ghost) {
      ctx.fillStyle = C.paper;
      ctx.fill();
      ctx.setLineDash([2 / k, 2 / k]);
      ctx.lineWidth = 1.2 / k;
      ctx.strokeStyle = C.ink3;
      ctx.stroke();
      ctx.restore();
      return;
    }
    ctx.fillStyle = C.sheet;
    ctx.fill();
    const screenR = r * k;
    const img = screenR >= 6.5 || n.depth === 0 || selected || hovered ? this.photo(n) : null;
    if (img) {
      ctx.save();
      ctx.beginPath();
      ctx.arc(n.cx, n.cy, r, 0, TAU);
      ctx.clip();
      const s = Math.min(img.naturalWidth, img.naturalHeight) || 1;
      const sx = (img.naturalWidth - s) / 2, sy = (img.naturalHeight - s) / 2;
      try {
        ctx.drawImage(img, sx, sy, s, s, n.cx - r, n.cy - r, r * 2, r * 2);
      } catch {
        /* a broken image simply falls back to the plain marker */
      }
      ctx.restore();
    } else if (screenR >= 8) {
      ctx.fillStyle = C.ink2;
      ctx.font = `600 ${r * 0.82}px ${this.family}`;
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      ctx.fillText(initials(n.name), n.cx, n.cy + r * 0.04);
    }
    ctx.lineWidth = (n.depth === 0 ? 2.2 : 1.3) / k;
    ctx.strokeStyle = n.depth === 0 ? C.ink : n.depth === 1 ? C.ink2 : C.ink3;
    if (n.outside) ctx.setLineDash([2.5 / k, 2 / k]);
    ctx.beginPath();
    ctx.arc(n.cx, n.cy, r, 0, TAU);
    ctx.stroke();
    ctx.setLineDash([]);
    ctx.restore();
  }

  drawSectorLabels(ctx, C, k) {
    const rimR = this.rimRadius();
    ctx.save();
    ctx.font = `600 13px ${this.family}`;
    ctx.textBaseline = "middle";
    // Every lab member's sector is labelled. Larger sectors are placed first; a label that would collide moves
    // outward along its sector's middle, with a short leader line back to the rim.
    const placed = [];
    this.sectorBoxes = [];
    const hits = (r) => placed.some((q) => r[0] < q[2] && r[2] > q[0] && r[1] < q[3] && r[3] > q[1]);
    for (const s of [...this.sectors].sort((a, b) => b.count - a.count)) {
      const span = s.a1 - s.a0;
      const labMember = this.mode === "lab" && s.id;
      if (!labMember && span * rimR * k < 40) continue;
      const mid = (s.a0 + s.a1) / 2;
      const label = labMember ? `${firstName(s.name)} (${s.count})` : `${s.name} (${s.count})`;
      const align = Math.cos(mid) > 0.25 ? "left" : Math.cos(mid) < -0.25 ? "right" : "center";
      const w = ctx.measureText(label).width;
      for (const off of [18, 36, 54, 72]) {
        const [wx, wy] = polar(mid, rimR + off / k);
        const x = wx * k + this.tx, y = wy * k + this.ty;
        const left = align === "left" ? x : align === "right" ? x - w : x - w / 2;
        const rect = [left - 3, y - 9, left + w + 3, y + 9];
        if (hits(rect) && off < 72) continue;
        placed.push(rect);
        this.sectorBoxes.push({ x0: rect[0], y0: rect[1], x1: rect[2], y1: rect[3] });
        if (off > 18) {
          const [ax, ay] = polar(mid, rimR + 4 / k), [bx, by] = polar(mid, rimR + (off - 10) / k);
          ctx.beginPath();
          ctx.moveTo(ax * k + this.tx, ay * k + this.ty);
          ctx.lineTo(bx * k + this.tx, by * k + this.ty);
          ctx.strokeStyle = C.ink3;
          ctx.lineWidth = 1;
          ctx.stroke();
        }
        ctx.textAlign = align;
        haloText(ctx, label, x, y, C.paper, s.group ? C.ink : C.ink2);
        break;
      }
    }
    if (this.mode === "lab") {
      ctx.font = `12px ${this.family}`;
      ctx.textAlign = "left";
      for (const ring of this.rings) {
        const [wx, wy] = polar(START - 0.02, ring.r);
        const x = wx * k + this.tx + 6, y = wy * k + this.ty - 8;
        if (ring.r * k < 30) continue;
        haloText(ctx, ring.label, x, y, C.paper, C.contourLabel);
        this.sectorBoxes.push({ x0: x - 2, y0: y - 8, x1: x + ctx.measureText(ring.label).width + 2, y1: y + 8 });
      }
    }
    ctx.restore();
  }

  drawLabels(ctx, C, k) {
    const family = this.family;
    const focus = this.focus;
    const routeNodes = new Set();
    if (focus) for (const key of focus.routeLinks) for (const id of key.split("|")) routeNodes.add(id);
    const many = this.highlight && this.highlight.size > 80;
    // Priority decides who keeps a label when labels would collide (lower number wins).
    const prio = (n) => {
      if (n.id === this.sel) return 0;
      if (n.depth === 0 || (this.mode === "ego" && n.id === this.center)) return 1;
      if (n.id === this.hover) return 1.5;
      if (routeNodes.has(n.id)) return 2;
      if (focus && focus.nodes.has(n.id)) return 3 + (n.degree ?? 5) * 0.1;
      if (this.highlight && this.highlight.has(n.id) && !many) return 4;
      return 9;
    };
    const cands = [];
    const outerOn = this.outerLabels(k);
    for (const n of this.nodes) {
      if (n.ghost && n.id !== this.hover && !routeNodes.has(n.id) && n.id !== this.sel) continue;
      const p = prio(n);
      const onOuterRing = this.mode === "lab" && Math.abs((n.R || 0) - (this.outerR || 0)) < 1;
      const fits = n.span * n.R * k >= LABEL_PX - 1 && (!onOuterRing || outerOn);
      if (p === 9 && (!fits || this.isFaded(n))) continue;
      const x = n.cx * k + this.tx, y = n.cy * k + this.ty;
      if (x < -200 || y < -50 || x > this.width + 200 || y > this.height + 50) continue;
      cands.push({ n, p, fits, x, y });
    }
    cands.sort((a, b) => a.p - b.p);
    // Sector labels are drawn first; person labels keep clear of them (except the selected or pointed-at person).
    const placed = [...(this.sectorBoxes || [])];
    const hit = (r) => placed.some((q) => r.x0 < q.x1 && r.x1 > q.x0 && r.y0 < q.y1 && r.y1 > q.y0);
    for (const { n, p, fits, x, y } of cands) {
      const faded = this.isFaded(n);
      const bold = n.id === this.sel || n.depth === 0;
      const gap = this.rad(n) * k + 5;
      if ((n.depth === 0 && this.mode === "lab") || (this.mode === "ego" && n.id === this.center)) {
        const text = this.mode === "lab" ? firstName(n.name) : n.name;
        ctx.font = `${this.mode === "ego" ? 700 : 600} ${this.mode === "ego" ? 14 : 13}px ${family}`;
        ctx.textAlign = "center";
        ctx.textBaseline = "top";
        const w = ctx.measureText(text).width;
        const rect = { x0: x - w / 2 - 2, x1: x + w / 2 + 2, y0: y + gap - 2, y1: y + gap + 16 };
        // Lab names are placed one by one; a name that would cover another appears on hover instead.
        if (hit(rect) && n.id !== this.sel && n.id !== this.hover) continue;
        placed.push(rect);
        haloText(ctx, text, x, y + gap - 2, C.paper, C.ink);
        continue;
      }
      ctx.font = `${bold ? 600 : 500} ${LABEL_PX}px ${family}`;
      if (p < 9 && !(fits && p >= 2)) {
        // Horizontal label beside the node, skipped if it would cover a more important one.
        const right = Math.cos(n.a) >= 0;
        const w = ctx.measureText(n.name).width;
        const x0 = right ? x + gap : x - gap - w;
        const rect = { x0: x0 - 2, x1: x0 + w + 2, y0: y - 8, y1: y + 8 };
        if (p > 1.5 && hit(rect)) continue;
        placed.push(rect);
        ctx.textAlign = right ? "left" : "right";
        ctx.textBaseline = "middle";
        haloText(ctx, n.name, x + (right ? gap : -gap), y, C.paper, faded ? C.ink3 : C.ink);
        continue;
      }
      // Radial label reading outward, flipped on the left half so it stays upright.
      ctx.save();
      ctx.translate(x, y);
      const flip = Math.cos(n.a) < 0;
      ctx.rotate(flip ? n.a + Math.PI : n.a);
      ctx.textAlign = flip ? "right" : "left";
      ctx.textBaseline = "middle";
      haloText(ctx, n.name, flip ? -gap : gap, 0, C.paper, faded ? C.ink3 : n.depth >= 2 ? C.ink2 : C.ink);
      ctx.restore();
    }
  }
}

function spreadAngles(angles, minSep) {
  const a = angles.slice();
  const n = a.length;
  if (n < 2) return a;
  for (let it = 0; it < 300; it++) {
    let moved = false;
    for (let i = 0; i < n; i++) {
      const j = (i + 1) % n;
      let d = a[j] - a[i];
      if (j === 0) d += TAU;
      if (d < minSep - 1e-6) {
        const push = (minSep - d) / 2;
        a[i] -= push;
        a[j] += push;
        moved = true;
      }
    }
    if (!moved) break;
  }
  return a;
}

function haloText(ctx, text, x, y, halo, fill) {
  ctx.lineJoin = "round";
  ctx.lineWidth = 4;
  ctx.strokeStyle = halo;
  ctx.strokeText(text, x, y);
  ctx.fillStyle = fill;
  ctx.fillText(text, x, y);
}

function firstName(name) {
  return (name || "").split(/\s+/)[0];
}

function ordinal(d) {
  const sfx = { 1: "st", 2: "nd", 3: "rd" }[d] || "th";
  return `${d}${sfx}`;
}

function groupName(g) {
  return { supervision: "Supervision", research: "Research", association: "Association" }[g] || g;
}

function countDesc(id, children) {
  let n = 0;
  for (const c of children.get(id) || []) n += 1 + countDesc(c.id, children);
  return n;
}
