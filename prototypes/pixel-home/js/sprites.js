'use strict';

// ---------------------------------------------------------------------------
// Sprite bakery, "cozy lofi" edition.  Every visual is drawn once at native
// pixel scale into an offscreen Surface and reused each frame.
//
// Furniture sprites use a local iso frame whose origin is the projected
// position of the object's grid cell (x, y) at z=0 — the north corner of its
// footprint.  The engine places the sprite by projecting that grid point and
// adding `offset`.  Optional fields:
//   front    – overlay drawn above the actor (chair backs etc.)
//   emitters – [{dx, dy, z, r, color, pool}] warm light sources at night
// ---------------------------------------------------------------------------

const Bakery = (() => {

  // ---- shared bits ----------------------------------------------------------

  function frame(w, d, hPx, extraTop = 0) {
    const width = (w + d) * HX + 4;
    const height = (w + d) * HY + hPx + 4 + extraTop;
    const sf = new Surface(width, height);
    const ox = d * HX + 2, oy = hPx + 2 + extraTop;
    return { sf, ox, oy, P: (x, y, z) => isoPoint(ox, oy, x, y, z) };
  }

  function bakeBox(w, d, h, top, side, opts = {}) {
    const { sf, ox, oy, P } = frame(w, d, h * HZ);
    const zTop = h;
    const topPts = [P(0, 0, zTop), P(w, 0, zTop), P(w, d, zTop), P(0, d, zTop)];
    sf.poly([P(0, d, zTop), P(w, d, zTop), P(w, d, 0), P(0, d, 0)], shade(side, 0.78));
    sf.poly([P(w, 0, zTop), P(w, d, zTop), P(w, d, 0), P(w, 0, 0)], shade(side, 0.56));
    sf.poly(topPts, top);
    if (opts.topShine !== false) sf.poly([P(0, 0, zTop), P(w, 0, zTop), P(w - 0.3, 0.3, zTop), P(0.3, 0.3, zTop)], shade(top, 1.1));
    sf.polyLine(topPts, PAL.outline);
    sf.polyLine([P(0, d, zTop), P(0, d, 0), P(w, d, 0), P(w, d, zTop)], PAL.outline);
    sf.polyLine([P(w, 0, zTop), P(w, 0, 0), P(w, d, 0)], PAL.outline);
    return { surface: sf, offset: [-ox, -oy], w, d, h };
  }

  // small potted plant drawn into an existing surface at pixel (px, py) base
  function potPlantInto(sf, px, py, seed, size = 1) {
    const rng = mulberry32(seed);
    const pw = Math.round(4 * size);
    sf.rect(px - pw, py - 4 * size, pw * 2, 4 * size, PAL.potClay);
    sf.rect(px - pw - 1, py - 5 * size, pw * 2 + 2, 2, shade(PAL.potClay, 1.2));
    sf.rect(px - pw, py - 1, pw * 2, 1, shade(PAL.potClay, 0.65));
    const leaves = [PAL.leafDark, PAL.leaf, PAL.leafLite];
    for (let i = 0; i < 10 * size; i += 1) {
      const ang = rng() * Math.PI * 2, r = rng() * 4 * size;
      const ly = -5 * size - rng() * 7 * size;
      sf.rect(px + Math.cos(ang) * r - 1, py + ly, 2, 2, leaves[Math.floor(rng() * 3)]);
    }
  }

  function booksRowInto(sf, P, x0, x1, y, z, seed, lean = true) {
    // vertical book spines along the +x axis on a shelf at height z
    const rng = mulberry32(seed);
    const colors = [PAL.teal, PAL.rose, PAL.sage, PAL.gold, PAL.navy, PAL.cream, PAL.roseDark];
    let x = x0;
    while (x < x1 - 0.06) {
      const bw = 0.10 + rng() * 0.08, bh = 0.30 + rng() * 0.14;
      const color = colors[Math.floor(rng() * colors.length)];
      if (rng() < 0.86) {
        const pts = [P(x, y, z + bh), P(x + bw, y, z + bh), P(x + bw, y, z), P(x, y, z)];
        sf.poly(pts, color);
        sf.line(...P(x, y, z + bh), ...P(x, y, z), shade(color, 0.65));
        sf.line(...P(x, y, z + bh), ...P(x + bw, y, z + bh), shade(color, 1.18));
      } else if (lean && rng() < 0.5) {
        // a leaning book
        const pts = [P(x, y, z + 0.26), P(x + 0.16, y, z + 0.20), P(x + 0.16, y, z), P(x, y, z)];
        sf.poly(pts, colors[Math.floor(rng() * colors.length)]);
      }
      x += bw + 0.015;
    }
  }

  // ---- floor / rug ----------------------------------------------------------

  // One wooden plank row segment inside a tile: planks run along +x.
  function bakeFloorTile(gx, gy, zone, rowSeed) {
    const sf = new Surface(TILE_W + 2, TILE_H + 2);
    const P = (x, y) => [HX + 1 + (x - y) * HX, 1 + (x + y) * HY];
    const pts = [P(0, 0), P(1, 0), P(1, 1), P(0, 1)];
    if (zone === 'tile') {
      const base = (gx + gy) % 2 === 0 ? PAL.kitchenA : PAL.kitchenB;
      sf.poly(pts, base);
      sf.polyLine(pts, shade(base, 0.8));
      const c = P(0.5, 0.5);
      sf.rect(c[0] - 1, c[1], 2, 1, shade(base, 1.1));
      return sf;
    }
    const rng = mulberry32(rowSeed * 977 + gx * 131 + 7);
    // two planks per tile, running along x; hue follows the row so planks
    // continue across tile seams.  Seams are low-contrast: commercial pixel
    // floors read as a soft weave, not stripes.
    for (const half of [0, 1]) {
      const rowRng = mulberry32((gy * 2 + half) * 419 + 11);
      const jitter = 0.95 + rowRng() * 0.11;
      const base = shade(half === 0 ? PAL.floorA : PAL.floorB, jitter);
      const quad = [P(0, half * 0.5), P(1, half * 0.5), P(1, half * 0.5 + 0.5), P(0, half * 0.5 + 0.5)];
      sf.poly(quad, base);
      sf.dither(quad, shade(base, 0.93), 0.035, rng);
      sf.dither(quad, shade(base, 1.06), 0.02, rng);
      // plank seam along the row: subtle darker step, no highlight line
      sf.line(...quad[3], ...quad[2], shade(base, 0.88));
      // occasional butt joint, softer than the row seam
      if (rng() < 0.28) {
        const t = 0.25 + rng() * 0.5;
        const a = P(t, half * 0.5), b = P(t, half * 0.5 + 0.5);
        sf.line(a[0], a[1] + 1, b[0], b[1], shade(base, 0.9));
      }
    }
    return sf;
  }

  // Whole-area patterned rug baked as one sprite (w×d tiles).
  // Structure follows woven rugs: field, thin guard stripe, patterned border
  // band, all strictly aligned to the iso axes.
  function bakeRug(w, d, inner, border, seed) {
    const { sf, ox, oy, P } = frame(w, d, 0);
    const z = 0.02;
    const innerD = shade(inner, 0.88), innerL = shade(inner, 1.1);
    const borderD = shade(border, 0.82), borderL = shade(border, 1.18);
    const band = 0.32, guard = 0.42;
    const ring = inset => [P(inset, inset, z), P(w - inset, inset, z), P(w - inset, d - inset, z), P(inset, d - inset, z)];
    sf.poly(ring(0), border);
    sf.poly(ring(band), inner);
    // guard stripes: crisp 1px lines following the weave
    sf.polyLine(ring(band), borderD);
    sf.polyLine(ring(guard), innerD);
    // border motif: alternating pips centered in the band, on all four sides
    const pip = (x, y, color) => { const p = P(x, y, z); sf.rect(p[0] - 1, p[1], 2, 1, color); };
    for (let x = 0.5; x <= w - 0.5; x += 0.5) { pip(x, band / 2, borderL); pip(x, d - band / 2, borderL); }
    for (let y = 0.5; y <= d - 0.5; y += 0.5) { pip(band / 2, y, borderL); pip(w - band / 2, y, borderL); }
    // field motif: diamond lattice with alternating accents
    for (let x = 1.0; x <= w - 1.0; x += 0.5) {
      for (let y = 1.0; y <= d - 1.0; y += 0.5) {
        const even = Math.round((x + y) * 2) % 2 === 0;
        const c = P(x, y, z);
        if (even) {
          sf.set(c[0] - 1, c[1], innerD); sf.set(c[0] + 1, c[1], innerD);
          sf.set(c[0], c[1] - 1, innerD); sf.set(c[0], c[1] + 1, innerD);
        } else {
          sf.set(c[0], c[1], innerL);
        }
      }
    }
    // corner knots
    for (const [cx, cy] of [[band / 2, band / 2], [w - band / 2, band / 2], [band / 2, d - band / 2], [w - band / 2, d - band / 2]]) {
      const p = P(cx, cy, z);
      sf.rect(p[0] - 1, p[1] - 1, 3, 3, borderL);
      sf.set(p[0], p[1], borderD);
    }
    // fringe on the two viewer-facing edges, evenly spaced
    for (let x = 0.08; x < w; x += 0.16) {
      const p = P(x, d, z);
      sf.set(p[0], p[1] + 1, '#d9cbaa');
      sf.set(p[0], p[1] + 2, shade('#d9cbaa', 0.8));
    }
    for (let y = 0.08; y < d; y += 0.16) {
      const p = P(w, y, z);
      sf.set(p[0], p[1] + 1, '#d9cbaa');
      sf.set(p[0], p[1] + 2, shade('#d9cbaa', 0.8));
    }
    sf.polyLine(ring(0), borderD);
    return { surface: sf, offset: [-ox, -oy], w, d, h: 0.02 };
  }

  // ---- furniture -------------------------------------------------------------

  function bakeBed() {
    const w = 2, d = 3, h = 0.55;
    const { sf, ox, oy, P } = frame(w, d, 1.15 * HZ);
    // wooden frame box
    sf.poly([P(0, d, 0.42), P(w, d, 0.42), P(w, d, 0.06), P(0, d, 0.06)], shade(PAL.wood, 0.72));
    sf.poly([P(w, 0, 0.42), P(w, d, 0.42), P(w, d, 0.06), P(w, 0, 0.06)], shade(PAL.wood, 0.5));
    // legs
    for (const [lx, ly] of [[0.06, d - 0.1], [w - 0.12, d - 0.1], [w - 0.12, 0.2]]) {
      const a = P(lx, ly, 0.1), b = P(lx, ly, 0);
      sf.line(a[0], a[1], b[0], b[1], PAL.woodDeep);
      sf.line(a[0] + 1, a[1], b[0] + 1, b[1], PAL.woodDeep);
    }
    // mattress + fitted sheet (visible near pillow end)
    const mTop = [P(0.04, 0.04, h), P(w - 0.04, 0.04, h), P(w - 0.04, d - 0.04, h), P(0.04, d - 0.04, h)];
    sf.poly(mTop, PAL.linen);
    sf.polyLine(mTop, shade(PAL.linen, 0.7));
    // quilt from y=0.95 to foot, patchwork pattern like the reference
    const qz = h + 0.07, q0 = 0.95;
    const quilt = [P(0, q0, qz), P(w, q0, qz), P(w, d, qz), P(0, d, qz)];
    sf.poly(quilt, PAL.rose);
    // patch grid
    for (let qx = 0; qx < 4; qx += 1) {
      for (let qy = 0; qy < 4; qy += 1) {
        const px0 = qx * 0.5, py0 = q0 + qy * ((d - q0) / 4);
        const cell = [P(px0, py0, qz), P(px0 + 0.5, py0, qz), P(px0 + 0.5, py0 + (d - q0) / 4, qz), P(px0, py0 + (d - q0) / 4, qz)];
        if ((qx + qy) % 2 === 0) sf.poly(cell, shade(PAL.rose, 1.12));
        if ((qx * 3 + qy) % 5 === 0) sf.poly(cell, shade(PAL.blush, 1.0));
      }
    }
    // quilt stitch lines
    for (let qy = 1; qy < 4; qy += 1) {
      const yy = q0 + qy * ((d - q0) / 4);
      sf.line(...P(0.03, yy, qz), ...P(w - 0.03, yy, qz), shade(PAL.roseDark, 1.05));
    }
    sf.line(...P(0.5, q0, qz), ...P(0.5, d, qz), shade(PAL.roseDark, 1.05));
    sf.line(...P(1.0, q0, qz), ...P(1.0, d, qz), shade(PAL.roseDark, 1.05));
    sf.line(...P(1.5, q0, qz), ...P(1.5, d, qz), shade(PAL.roseDark, 1.05));
    // rolled edge at the top of the quilt
    sf.poly([P(0, q0, qz), P(w, q0, qz), P(w, q0 + 0.16, qz - 0.05), P(0, q0 + 0.16, qz - 0.05)], shade(PAL.cream, 1.02));
    sf.line(...P(0, q0 + 0.16, qz - 0.05), ...P(w, q0 + 0.16, qz - 0.05), shade(PAL.roseDark, 0.9));
    // quilt drapes over the left face
    const drape = [P(0, q0, qz), P(0, d, qz), P(0, d, 0.16), P(0, q0, 0.16)];
    sf.poly(drape, shade(PAL.rose, 0.8));
    sf.line(...P(0, q0, 0.7), ...P(0, d, 0.7), shade(PAL.roseDark, 0.85));
    // sage folded blanket at the foot
    const fz = qz + 0.05, f0 = d - 0.75;
    const fold = [P(0.03, f0, fz), P(w - 0.03, f0, fz), P(w - 0.03, d - 0.18, fz), P(0.03, d - 0.18, fz)];
    sf.poly(fold, PAL.sage);
    sf.poly([P(0.03, f0, fz), P(w - 0.03, f0, fz), P(w - 0.03, f0 + 0.14, fz - 0.04), P(0.03, f0 + 0.14, fz - 0.04)], shade(PAL.sage, 1.15));
    sf.line(...P(0.03, (f0 + d - 0.18) / 2, fz), ...P(w - 0.03, (f0 + d - 0.18) / 2, fz), PAL.sageDark);
    sf.polyLine(fold, shade(PAL.sageDark, 0.9));
    // pillows
    for (const [px, pw] of [[0.14, 0.78], [1.06, 0.78]]) {
      const pz = h + 0.13;
      const pts = [P(px, 0.14, pz), P(px + pw, 0.14, pz), P(px + pw, 0.62, pz), P(px, 0.62, pz)];
      sf.poly(pts, '#f7efdb');
      sf.poly([P(px, 0.14, pz), P(px + pw, 0.14, pz), P(px + pw - 0.08, 0.26, pz), P(px + 0.08, 0.26, pz)], '#fdf8ea');
      sf.polyLine(pts, shade('#f7efdb', 0.62));
    }
    // small accent cushion
    const ac = [P(0.72, 0.5, h + 0.16), P(1.3, 0.5, h + 0.16), P(1.3, 0.85, h + 0.13), P(0.72, 0.85, h + 0.13)];
    sf.poly(ac, PAL.roseDark);
    sf.polyLine(ac, shade(PAL.roseDark, 0.7));
    // headboard
    const hb = [P(0, 0, 1.05), P(w, 0, 1.05), P(w, 0, 0), P(0, 0, 0)];
    sf.poly(hb, PAL.wood);
    sf.poly([P(0, 0, 1.05), P(w, 0, 1.05), P(w, 0, 0.88), P(0, 0, 0.88)], PAL.woodLite);
    // vertical slats
    for (let sx = 0.3; sx < w; sx += 0.35) sf.line(...P(sx, 0, 0.86), ...P(sx, 0, 0.34), shade(PAL.wood, 0.8));
    sf.polyLine(hb, PAL.outline);
    sf.polyLine(quilt, PAL.outline);
    return { surface: sf, offset: [-ox, -oy], w, d, h };
  }

  function bakeSofa() {
    const w = 2, d = 1, h = 0.62;
    const { sf, ox, oy, P } = frame(w, d, 1.2 * HZ);
    const body = PAL.cream, bodyD = shade(PAL.cream, 0.74), bodyL = shade(PAL.cream, 1.07);
    // wooden feet
    for (const [lx, ly] of [[0.08, d - 0.08], [w - 0.1, d - 0.08]]) {
      const a = P(lx, ly, 0.12), b = P(lx, ly, 0);
      sf.line(a[0], a[1], b[0], b[1], PAL.woodDeep);
      sf.line(a[0] + 1, a[1], b[0] + 1, b[1], PAL.woodDeep);
    }
    // base
    sf.poly([P(0, d, 0.5), P(w, d, 0.5), P(w, d, 0.1), P(0, d, 0.1)], bodyD);
    sf.poly([P(w, 0, 0.5), P(w, d, 0.5), P(w, d, 0.1), P(w, 0, 0.1)], shade(PAL.cream, 0.6));
    // seat cushions
    for (const cx of [0.1, 1.02]) {
      const c = [P(cx, 0.18, 0.52), P(cx + 0.88, 0.18, 0.52), P(cx + 0.88, d - 0.04, 0.5), P(cx, d - 0.04, 0.5)];
      sf.poly(c, body);
      sf.poly([P(cx, 0.18, 0.52), P(cx + 0.88, 0.18, 0.52), P(cx + 0.76, 0.36, 0.52), P(cx + 0.12, 0.36, 0.52)], bodyL);
      sf.polyLine(c, shade(PAL.cream, 0.58));
      // front face of cushion
      sf.poly([P(cx, d - 0.04, 0.5), P(cx + 0.88, d - 0.04, 0.5), P(cx + 0.88, d - 0.04, 0.36), P(cx, d - 0.04, 0.36)], shade(PAL.cream, 0.86));
    }
    // backrest
    const back = [P(0, 0, 1.14), P(w, 0, 1.14), P(w, 0.24, 1.04), P(0, 0.24, 1.04)];
    sf.poly([P(0, 0, 1.14), P(w, 0, 1.14), P(w, 0, 0.24), P(0, 0, 0.24)], bodyD);
    sf.poly(back, body);
    sf.line(...P(1.0, 0.02, 1.13), ...P(1.0, 0.22, 1.03), shade(PAL.cream, 0.72));
    sf.polyLine(back, shade(PAL.cream, 0.55));
    // armrests
    for (const [x0, x1] of [[-0.04, 0.2], [w - 0.2, w + 0.04]]) {
      const arm = [P(x0, 0, 0.9), P(x1, 0, 0.9), P(x1, d, 0.9), P(x0, d, 0.9)];
      sf.poly([P(x0, d, 0.9), P(x1, d, 0.9), P(x1, d, 0.14), P(x0, d, 0.14)], bodyD);
      sf.poly([P(x1, 0, 0.9), P(x1, d, 0.9), P(x1, d, 0.14), P(x1, 0, 0.14)], shade(PAL.cream, 0.62));
      sf.poly(arm, bodyL);
      sf.polyLine(arm, shade(PAL.cream, 0.55));
    }
    // sage throw pillow (left) + rose draped blanket (right arm)
    const tp = [P(0.26, 0.16, 0.92), P(0.66, 0.16, 0.92), P(0.66, 0.48, 0.74), P(0.26, 0.48, 0.74)];
    sf.poly(tp, PAL.sage);
    sf.set(...P(0.46, 0.32, 0.83), shade(PAL.sage, 0.7));
    sf.polyLine(tp, PAL.sageDark);
    const bl = [P(1.28, 0.02, 0.95), P(1.9, 0.02, 0.95), P(1.98, d - 0.05, 0.62), P(1.36, d - 0.05, 0.62)];
    sf.poly(bl, PAL.blush);
    sf.line(...P(1.45, 0.1, 0.9), ...P(1.53, d - 0.1, 0.62), shade(PAL.blush, 0.82));
    sf.line(...P(1.62, 0.1, 0.9), ...P(1.7, d - 0.1, 0.62), shade(PAL.blush, 0.82));
    sf.poly([P(1.36, d - 0.05, 0.62), P(1.98, d - 0.05, 0.62), P(1.98, d - 0.05, 0.3), P(1.36, d - 0.05, 0.3)], shade(PAL.blush, 0.9));
    sf.polyLine(bl, shade(PAL.blush, 0.66));
    sf.polyLine([P(0, d, 0.5), P(w, d, 0.5), P(w, d, 0.1), P(0, d, 0.1)], PAL.outline);
    sf.line(...P(0, 0, 1.14), ...P(w, 0, 1.14), PAL.outline);
    return { surface: sf, offset: [-ox, -oy], w, d, h };
  }

  function bakeCoffeeTable() {
    const w = 2, d = 1, h = 0.42;
    const { sf, ox, oy, P } = frame(w, d, h * HZ + 6);
    // legs
    for (const [lx, ly] of [[0.12, 0.2], [w - 0.16, 0.2], [0.12, d - 0.24], [w - 0.16, d - 0.24]]) {
      const a = P(lx + 0.06, ly + 0.1, h - 0.02), b = P(lx + 0.06, ly + 0.1, 0);
      sf.line(a[0], a[1], b[0], b[1], PAL.woodDeep);
      sf.line(a[0] + 1, a[1], b[0] + 1, b[1], PAL.outline);
    }
    // top
    const top = [P(0, 0, h), P(w, 0, h), P(w, d, h), P(0, d, h)];
    sf.poly([P(0, d, h), P(w, d, h), P(w, d, h - 0.09), P(0, d, h - 0.09)], shade(PAL.wood, 0.78));
    sf.poly([P(w, 0, h), P(w, d, h), P(w, d, h - 0.09), P(w, 0, h - 0.09)], shade(PAL.wood, 0.55));
    sf.poly(top, PAL.wood);
    sf.poly([P(0, 0, h), P(w, 0, h), P(w - 0.24, 0.24, h), P(0.24, 0.24, h)], PAL.woodLite);
    sf.line(...P(0.1, 0.5, h), ...P(w - 0.1, 0.5, h), shade(PAL.wood, 0.88));
    sf.polyLine(top, PAL.outline);
    // tea set: pot + two cups + small vase
    const potC = P(0.7, 0.45, h);
    sf.ellipse(potC[0], potC[1] - 4, 4, 3, PAL.cream);
    sf.rect(potC[0] - 1, potC[1] - 9, 2, 2, PAL.cream);            // lid
    sf.rect(potC[0] + 4, potC[1] - 6, 3, 1, PAL.cream);            // spout
    sf.rect(potC[0] - 6, potC[1] - 6, 2, 3, shade(PAL.cream, 0.8)); // handle
    sf.ellipse(potC[0], potC[1] - 4, 4, 3, PAL.cream);
    sf.set(potC[0] - 2, potC[1] - 6, '#fff');
    for (const [cx, cy] of [[1.15, 0.32], [1.25, 0.62]]) {
      const c = P(cx, cy, h);
      sf.ellipse(c[0], c[1] - 1, 2, 1.4, PAL.cream);
      sf.set(c[0] + 2, c[1] - 1, shade(PAL.cream, 0.75));
    }
    const vase = P(1.65, 0.42, h);
    sf.rect(vase[0] - 1, vase[1] - 6, 3, 5, PAL.teal);
    sf.rect(vase[0], vase[1] - 9, 1, 3, PAL.leaf);
    sf.rect(vase[0] - 2, vase[1] - 10, 2, 2, PAL.blush);
    sf.rect(vase[0] + 1, vase[1] - 11, 2, 2, PAL.gold);
    return { surface: sf, offset: [-ox, -oy], w, d, h };
  }

  function drawChairBodyInto(sf, P, gx, gy, seatColor) {
    const h = 0.52;
    for (const [lx, ly] of [[0.26, 0.3], [0.72, 0.3], [0.26, 0.72], [0.72, 0.72]]) {
      const a = P(gx + lx, gy + ly, h), b = P(gx + lx, gy + ly, 0);
      sf.line(a[0], a[1], b[0], b[1], PAL.woodDeep);
    }
    const seat = [P(gx + 0.18, gy + 0.22, h), P(gx + 0.82, gy + 0.22, h), P(gx + 0.82, gy + 0.82, h), P(gx + 0.18, gy + 0.82, h)];
    sf.poly(seat, seatColor || PAL.wood);
    sf.poly([P(gx + 0.18, gy + 0.22, h), P(gx + 0.82, gy + 0.22, h), P(gx + 0.72, gy + 0.36, h), P(gx + 0.28, gy + 0.36, h)], shade(seatColor || PAL.wood, 1.14));
    sf.polyLine(seat, PAL.outline);
  }

  function drawChairBackInto(sf, P, gx, gy, side) {
    const h = 0.52, top = 1.28;
    const y = side === 's' ? gy + 0.82 : gy + 0.2;
    const back = [P(gx + 0.18, y, top), P(gx + 0.82, y, top), P(gx + 0.82, y, h - 0.06), P(gx + 0.18, y, h - 0.06)];
    sf.poly(back, PAL.wood);
    sf.poly([back[0], back[1], [back[1][0], back[1][1] + 3], [back[0][0], back[0][1] + 3]], PAL.woodLite);
    // slats
    sf.line(...P(gx + 0.5, y, top - 0.06), ...P(gx + 0.5, y, h + 0.1), shade(PAL.wood, 0.78));
    sf.polyLine(back, PAL.outline);
  }

  function bakeChair() {
    const w = 1, d = 1;
    const { sf, ox, oy, P } = frame(w, d, 1.4 * HZ);
    drawChairBackInto(sf, P, 0, 0, 'n');
    drawChairBodyInto(sf, P, 0, 0);
    return { surface: sf, offset: [-ox, -oy], w, d, h: 0.52 };
  }

  // Desk workstation 2×2: desk along x, green office chair, lamp, laptop, clutter.
  function bakeDeskSet() {
    const w = 2, d = 2;
    const { sf, ox, oy, P } = frame(w, d, 1.75 * HZ);
    const h = 1.0;
    // side drawers block (right end)
    const dr = { x: 1.42, y: 0.06, w: 0.54, dpt: 0.88 };
    sf.poly([P(dr.x, dr.y + dr.dpt, h - 0.04), P(dr.x + dr.w, dr.y + dr.dpt, h - 0.04), P(dr.x + dr.w, dr.y + dr.dpt, 0.04), P(dr.x, dr.y + dr.dpt, 0.04)], shade(PAL.wood, 0.72));
    sf.poly([P(dr.x + dr.w, dr.y, h - 0.04), P(dr.x + dr.w, dr.y + dr.dpt, h - 0.04), P(dr.x + dr.w, dr.y + dr.dpt, 0.04), P(dr.x + dr.w, dr.y, 0.04)], shade(PAL.wood, 0.52));
    // drawer fronts on the left face of the block
    for (let i = 0; i < 3; i += 1) {
      const z1 = 0.78 - i * 0.24, z0 = z1 - 0.18;
      const face = [P(dr.x + 0.05, dr.y + dr.dpt, z1), P(dr.x + dr.w - 0.05, dr.y + dr.dpt, z1), P(dr.x + dr.w - 0.05, dr.y + dr.dpt, z0), P(dr.x + 0.05, dr.y + dr.dpt, z0)];
      sf.poly(face, shade(PAL.wood, 0.85));
      sf.polyLine(face, shade(PAL.woodDeep, 1.0));
      const knob = P(dr.x + dr.w / 2, dr.y + dr.dpt, (z0 + z1) / 2);
      sf.rect(knob[0] - 1, knob[1], 2, 1, PAL.gold);
    }
    // legs at the far end
    for (const [lx, ly] of [[0.1, 0.2], [0.1, 0.8]]) {
      const a = P(lx, ly, h - 0.04), b = P(lx, ly, 0);
      sf.line(a[0], a[1], b[0], b[1], PAL.woodDeep);
      sf.line(a[0] + 1, a[1], b[0] + 1, b[1], PAL.outline);
    }
    // desktop
    const topPts = [P(-0.04, 0, h), P(w + 0.04, 0, h), P(w + 0.04, 1, h), P(-0.04, 1, h)];
    sf.poly([P(-0.04, 1, h), P(w + 0.04, 1, h), P(w + 0.04, 1, h - 0.1), P(-0.04, 1, h - 0.1)], shade(PAL.wood, 0.78));
    sf.poly([P(w + 0.04, 0, h), P(w + 0.04, 1, h), P(w + 0.04, 1, h - 0.1), P(w + 0.04, 0, h - 0.1)], shade(PAL.wood, 0.55));
    sf.poly(topPts, PAL.wood);
    sf.poly([P(-0.04, 0, h), P(w + 0.04, 0, h), P(w - 0.2, 0.22, h), P(0.16, 0.22, h)], PAL.woodLite);
    sf.polyLine(topPts, PAL.outline);
    // laptop with glowing screen
    const lp = [P(0.72, 0.4, h + 0.01), P(1.24, 0.4, h + 0.01), P(1.24, 0.78, h + 0.01), P(0.72, 0.78, h + 0.01)];
    sf.poly(lp, '#57616e');
    sf.polyLine(lp, PAL.outline);
    const scr = [P(0.72, 0.4, h + 0.44), P(1.24, 0.4, h + 0.44), P(1.24, 0.4, h + 0.02), P(0.72, 0.4, h + 0.02)];
    sf.poly(scr, '#3c4552');
    sf.poly([P(0.76, 0.4, h + 0.4), P(1.2, 0.4, h + 0.4), P(1.2, 0.4, h + 0.07), P(0.76, 0.4, h + 0.07)], '#8fd0c6');
    sf.rect(...P(0.83, 0.4, h + 0.3), 6, 1, '#c3ece4');
    sf.rect(...P(0.83, 0.4, h + 0.22), 9, 1, '#aadfd4');
    sf.polyLine(scr, PAL.outline);
    // brass desk lamp (left end) — arm + shade, glow handled by emitter
    const base = P(0.28, 0.3, h);
    sf.ellipse(base[0], base[1] - 1, 3, 1.6, '#8c6b3f');
    sf.line(base[0], base[1] - 2, base[0] + 3, base[1] - 10, PAL.gold);
    sf.line(base[0] + 3, base[1] - 10, base[0] + 7, base[1] - 13, PAL.gold);
    sf.poly([[base[0] + 5, base[1] - 15], [base[0] + 11, base[1] - 15], [base[0] + 10, base[1] - 11], [base[0] + 6, base[1] - 11]], PAL.gold);
    sf.line(base[0] + 6, base[1] - 11, base[0] + 10, base[1] - 11, '#f4d491');
    // books + mug + papers
    const bx = P(1.62, 0.26, h);
    sf.rect(bx[0] - 6, bx[1] - 3, 12, 2, PAL.navy);
    sf.rect(bx[0] - 5, bx[1] - 5, 10, 2, PAL.rose);
    sf.rect(bx[0] - 4, bx[1] - 7, 9, 2, PAL.sage);
    sf.rect(bx[0] - 6, bx[1] - 3, 12, 1, shade(PAL.navy, 1.3));
    const mug = P(1.5, 0.68, h);
    sf.rect(mug[0] - 2, mug[1] - 4, 4, 4, PAL.teal);
    sf.rect(mug[0] + 2, mug[1] - 3, 1, 2, PAL.teal);
    const paper = P(0.5, 0.62, h + 0.005);
    sf.poly([[paper[0], paper[1]], [paper[0] + 8, paper[1] + 2], [paper[0] + 4, paper[1] + 5], [paper[0] - 4, paper[1] + 3]], '#f4ecd8');
    sf.line(paper[0] - 1, paper[1] + 2, paper[0] + 4, paper[1] + 3, shade('#f4ecd8', 0.7));
    // green office chair at (0.5, 1)
    const cg = { x: 0.5, y: 1.05 };
    const seatZ = 0.55;
    const post = P(cg.x + 0.5, cg.y + 0.5, 0);
    // star base
    for (const [dx, dy] of [[-5, 0], [5, 0], [-2, 2], [2, 2], [0, -3]]) sf.line(post[0], post[1] - 3, post[0] + dx, post[1] + (dy > 0 ? dy - 1 : dy), '#4a4550');
    sf.line(post[0], post[1] - 8, post[0], post[1] - 3, '#635d6b');
    const seat = [P(cg.x + 0.16, cg.y + 0.2, seatZ), P(cg.x + 0.84, cg.y + 0.2, seatZ), P(cg.x + 0.84, cg.y + 0.86, seatZ - 0.03), P(cg.x + 0.16, cg.y + 0.86, seatZ - 0.03)];
    sf.poly(seat, '#5f8a6d');
    sf.poly([P(cg.x + 0.16, cg.y + 0.2, seatZ), P(cg.x + 0.84, cg.y + 0.2, seatZ), P(cg.x + 0.72, cg.y + 0.34, seatZ), P(cg.x + 0.28, cg.y + 0.34, seatZ)], '#729a7d');
    sf.polyLine(seat, PAL.outline);
    sf.poly([P(cg.x + 0.16, cg.y + 0.86, seatZ - 0.03), P(cg.x + 0.84, cg.y + 0.86, seatZ - 0.03), P(cg.x + 0.84, cg.y + 0.86, seatZ - 0.16), P(cg.x + 0.16, cg.y + 0.86, seatZ - 0.16)], shade('#5f8a6d', 0.8));
    const front = new Surface(sf.w, sf.h);
    const backPts = [P(cg.x + 0.18, cg.y + 0.88, 1.42), P(cg.x + 0.82, cg.y + 0.88, 1.42), P(cg.x + 0.82, cg.y + 0.88, seatZ + 0.06), P(cg.x + 0.18, cg.y + 0.88, seatZ + 0.06)];
    front.poly(backPts, '#5f8a6d');
    front.poly([backPts[0], backPts[1], [backPts[1][0], backPts[1][1] + 4], [backPts[0][0], backPts[0][1] + 4]], '#729a7d');
    front.line(...P(cg.x + 0.5, cg.y + 0.88, 1.36), ...P(cg.x + 0.5, cg.y + 0.88, seatZ + 0.12), shade('#5f8a6d', 0.82));
    front.polyLine(backPts, PAL.outline);
    return {
      surface: sf, offset: [-ox, -oy], w, d, h,
      front: { surface: front, offset: [-ox, -oy] },
      emitters: [{ dx: 0.28 + 0.22, dy: 0.3, z: 0.9, r: 26, color: [255, 208, 130], pool: true }],
    };
  }

  // Dining nook 2×2: table with runner + vase + two place settings,
  // chair below (sitter) + chair behind (cosmetic).
  function bakeDiningSet() {
    const w = 2, d = 2;
    const { sf, ox, oy, P } = frame(w, d, 1.6 * HZ);
    const h = 0.74;
    // cosmetic chair on the north side (drawn before table)
    drawChairBackInto(sf, P, 0.94, -0.72, 'n');
    drawChairBodyInto(sf, P, 0.94, -0.66, PAL.wood);
    // table: four legs + slab top spanning (0.1..1.9, 0.15..1.05)
    const t = { x0: 0.12, x1: 1.88, y0: 0.18, y1: 1.04 };
    for (const [lx, ly] of [[t.x0 + 0.08, t.y0 + 0.1], [t.x1 - 0.12, t.y0 + 0.1], [t.x0 + 0.08, t.y1 - 0.14], [t.x1 - 0.12, t.y1 - 0.14]]) {
      const a = P(lx, ly, h - 0.03), b = P(lx, ly, 0);
      sf.line(a[0], a[1], b[0], b[1], PAL.woodDeep);
      sf.line(a[0] + 1, a[1], b[0] + 1, b[1], PAL.outline);
    }
    const top = [P(t.x0, t.y0, h), P(t.x1, t.y0, h), P(t.x1, t.y1, h), P(t.x0, t.y1, h)];
    sf.poly([P(t.x0, t.y1, h), P(t.x1, t.y1, h), P(t.x1, t.y1, h - 0.08), P(t.x0, t.y1, h - 0.08)], shade(PAL.wood, 0.78));
    sf.poly([P(t.x1, t.y0, h), P(t.x1, t.y1, h), P(t.x1, t.y1, h - 0.08), P(t.x1, t.y0, h - 0.08)], shade(PAL.wood, 0.55));
    sf.poly(top, PAL.wood);
    sf.poly([P(t.x0, t.y0, h), P(t.x1, t.y0, h), P(t.x1 - 0.2, t.y0 + 0.2, h), P(t.x0 + 0.2, t.y0 + 0.2, h)], PAL.woodLite);
    sf.polyLine(top, PAL.outline);
    // runner
    sf.poly([P(0.75, t.y0 + 0.04, h + 0.01), P(1.25, t.y0 + 0.04, h + 0.01), P(1.25, t.y1 - 0.04, h + 0.01), P(0.75, t.y1 - 0.04, h + 0.01)], PAL.sage);
    // vase of flowers
    const vc = P(1.0, 0.42, h);
    sf.rect(vc[0] - 2, vc[1] - 7, 4, 6, '#d8c8a8');
    sf.rect(vc[0] - 1, vc[1] - 8, 2, 1, shade('#d8c8a8', 0.8));
    sf.rect(vc[0] - 1, vc[1] - 11, 1, 4, PAL.leaf);
    sf.rect(vc[0] + 1, vc[1] - 10, 1, 3, PAL.leafDark);
    sf.rect(vc[0] - 3, vc[1] - 13, 2, 2, PAL.gold);
    sf.rect(vc[0], vc[1] - 14, 2, 2, PAL.gold);
    sf.rect(vc[0] + 2, vc[1] - 12, 2, 2, PAL.blush);
    // two place settings
    for (const [px, py] of [[1.0, 0.78], [1.35, 0.5]]) {
      const c = P(px, py, h + 0.01);
      sf.ellipse(c[0], c[1], 4, 2, '#efe6d0');
      sf.ellipse(c[0], c[1], 2.4, 1.2, shade('#efe6d0', 0.82));
      sf.set(c[0] - 5, c[1], shade('#efe6d0', 0.7));
    }
    // cup of tea
    const cup = P(0.62, 0.7, h);
    sf.rect(cup[0] - 1, cup[1] - 3, 3, 3, PAL.teal);
    // sitter chair (south)
    drawChairBodyInto(sf, P, 0.5, 1.06, PAL.wood);
    const front = new Surface(sf.w, sf.h);
    const Pf = (x, y, z) => isoPoint(ox, oy, x, y, z);
    drawChairBackInto(front, Pf, 0.5, 1.06, 's');
    return { surface: sf, offset: [-ox, -oy], w, d, h, front: { surface: front, offset: [-ox, -oy] } };
  }

  function bakeKitchen() {
    const w = 3, d = 1, h = 0.95;
    const { sf, ox, oy, P } = frame(w, d, 1.35 * HZ);
    // teal cabinet body
    sf.poly([P(0, d, h - 0.06), P(w, d, h - 0.06), P(w, d, 0.05), P(0, d, 0.05)], PAL.teal);
    sf.poly([P(w, 0, h - 0.06), P(w, d, h - 0.06), P(w, d, 0.05), P(w, 0, 0.05)], shade(PAL.teal, 0.66));
    // kick shadow
    sf.poly([P(0, d, 0.12), P(w, d, 0.12), P(w, d, 0.03), P(0, d, 0.03)], shade(PAL.teal, 0.5));
    // counter top slab
    const top = [P(-0.05, -0.05, h), P(w + 0.05, -0.05, h), P(w + 0.05, d + 0.05, h), P(-0.05, d + 0.05, h)];
    sf.poly([P(-0.05, d + 0.05, h), P(w + 0.05, d + 0.05, h), P(w + 0.05, d + 0.05, h - 0.09), P(-0.05, d + 0.05, h - 0.09)], shade('#e6d9bd', 0.8));
    sf.poly([P(w + 0.05, -0.05, h), P(w + 0.05, d + 0.05, h), P(w + 0.05, d + 0.05, h - 0.09), P(w + 0.05, -0.05, h - 0.09)], shade('#e6d9bd', 0.6));
    sf.poly(top, '#e6d9bd');
    sf.poly([P(-0.05, -0.05, h), P(w + 0.05, -0.05, h), P(w - 0.2, 0.2, h), P(0.15, 0.2, h)], '#f2e8cf');
    sf.polyLine(top, PAL.outline);
    // stove: two burners left
    for (const [bx, by] of [[0.45, 0.34], [1.05, 0.62]]) {
      const bc = P(bx, by, h + 0.01);
      sf.ellipse(bc[0], bc[1], 6, 3, '#43434c');
      sf.ellipse(bc[0], bc[1], 4, 2, '#2f2f38');
      sf.set(bc[0] - 3, bc[1] - 1, '#5a5a66');
    }
    // kettle on rear burner
    const kt = P(0.45, 0.34, h + 0.02);
    sf.ellipse(kt[0], kt[1] - 3, 4, 3, '#ece2cc');
    sf.rect(kt[0] - 1, kt[1] - 8, 2, 2, '#ece2cc');
    sf.rect(kt[0] + 4, kt[1] - 5, 2, 1, '#ece2cc');
    sf.line(kt[0] - 3, kt[1] - 7, kt[0] + 3, kt[1] - 7, shade('#ece2cc', 0.7));
    // sink right with faucet
    const sink = [P(2.1, 0.24, h), P(2.8, 0.24, h), P(2.8, 0.78, h), P(2.1, 0.78, h)];
    sf.poly(sink, '#aebfc2');
    sf.poly([P(2.16, 0.3, h), P(2.74, 0.3, h), P(2.74, 0.72, h), P(2.16, 0.72, h)], '#93a6ab');
    sf.polyLine(sink, shade('#93a6ab', 0.6));
    const tap = P(2.84, 0.5, h);
    sf.rect(tap[0] - 1, tap[1] - 8, 2, 8, '#8f9ba2');
    sf.rect(tap[0] - 5, tap[1] - 9, 5, 2, '#8f9ba2');
    sf.set(tap[0] - 5, tap[1] - 7, '#b9c4ca');
    // cabinet doors + drawer on the front face
    for (let i = 0; i < 3; i += 1) {
      const door = [P(i + 0.1, d, h - 0.16), P(i + 0.9, d, h - 0.16), P(i + 0.9, d, 0.16), P(i + 0.1, d, 0.16)];
      sf.poly(door, shade(PAL.teal, 0.88));
      sf.polyLine(door, shade(PAL.tealDark, 0.9));
      const inner = [P(i + 0.2, d, h - 0.28), P(i + 0.8, d, h - 0.28), P(i + 0.8, d, 0.26), P(i + 0.2, d, 0.26)];
      sf.polyLine(inner, shade(PAL.teal, 0.72));
      const knob = P(i + 0.78, d, (h) / 2);
      sf.rect(knob[0], knob[1], 2, 2, PAL.gold);
    }
    // towel on middle door
    const tw = [P(1.3, d, h - 0.2), P(1.62, d, h - 0.2), P(1.62, d, 0.42), P(1.3, d, 0.42)];
    sf.poly(tw, PAL.blush);
    sf.line(...P(1.3, d, h - 0.34), ...P(1.62, d, h - 0.34), shade(PAL.blush, 0.8));
    sf.polyLine(tw, shade(PAL.blush, 0.7));
    sf.polyLine([P(0, d, h - 0.06), P(w, d, h - 0.06), P(w, d, 0.05), P(0, d, 0.05)], PAL.outline);
    return { surface: sf, offset: [-ox, -oy], w, d, h };
  }

  function bakeFridge() {
    const w = 1, d = 1, h = 1.9;
    const base = bakeBox(w, d, h, '#e7e0ce', '#dfd6c2', { topShine: false });
    const sf = base.surface, ox = -base.offset[0], oy = -base.offset[1];
    const P = (x, y, z) => isoPoint(ox, oy, x, y, z);
    sf.line(...P(0, 1, h * 0.6), ...P(1, 1, h * 0.6), shade('#dfd6c2', 0.62));
    sf.line(...P(0.14, 1, h * 0.64), ...P(0.14, 1, h * 0.9), '#8f9ba2');
    sf.line(...P(0.14, 1, h * 0.3), ...P(0.14, 1, h * 0.54), '#8f9ba2');
    // magnets + photo + note
    const note = P(0.5, 1, h * 0.8);
    sf.rect(note[0] - 2, note[1], 5, 5, '#f6e9a8');
    sf.set(note[0], note[1] + 1, shade('#f6e9a8', 0.7));
    const photo = P(0.72, 1, h * 0.76);
    sf.rect(photo[0], photo[1], 4, 5, '#fff');
    sf.rect(photo[0] + 1, photo[1] + 1, 2, 2, PAL.teal);
    sf.set(P(0.3, 1, h * 0.5)[0], P(0.3, 1, h * 0.5)[1], PAL.rose);
    sf.set(P(0.62, 1, h * 0.44)[0], P(0.62, 1, h * 0.44)[1], PAL.gold);
    // plant on top
    const pc = P(0.5, 0.5, h);
    potPlantInto(sf, pc[0], pc[1] + 2, 31, 0.8);
    return { ...base, surface: sf };
  }

  function bakeBookcase() {
    const w = 1, d = 2, h = 2.15;
    const base = bakeBox(w, d, h, PAL.woodDark, PAL.wood, { topShine: false });
    const sf = base.surface, ox = -base.offset[0], oy = -base.offset[1];
    const P = (x, y, z) => isoPoint(ox, oy, x, y, z);
    // shelves carved into the +x face
    for (let shelf = 0; shelf < 3; shelf += 1) {
      const z0 = 0.24 + shelf * 0.62, z1 = z0 + 0.46;
      const inner = [P(w, 0.1, z1), P(w, d - 0.1, z1), P(w, d - 0.1, z0), P(w, 0.1, z0)];
      sf.poly(inner, shade(PAL.woodDeep, 0.72));
      // shelf lip
      sf.line(...P(w, 0.1, z0), ...P(w, d - 0.1, z0), shade(PAL.woodLite, 0.9));
      if (shelf === 2) {
        // top shelf: books + tiny plant
        booksRowIntoY(sf, P, 0.18, d - 0.55, z0 + 0.02, 51);
        const pp = P(w, d - 0.32, z0 + 0.02);
        potPlantInto(sf, pp[0], pp[1], 77, 0.62);
      } else if (shelf === 1) {
        booksRowIntoY(sf, P, 0.2, d - 0.2, z0 + 0.02, 52);
      } else {
        // bottom: storage boxes
        for (const [by, color] of [[0.22, PAL.sage], [0.92, PAL.rose]]) {
          const bx = [P(w, by, z0 + 0.34), P(w, by + 0.6, z0 + 0.34), P(w, by + 0.6, z0 + 0.02), P(w, by, z0 + 0.02)];
          sf.poly(bx, shade(color, 0.92));
          sf.polyLine(bx, shade(color, 0.6));
          const hh = P(w, by + 0.3, z0 + 0.2);
          sf.rect(hh[0] - 1, hh[1], 3, 1, shade(color, 0.5));
        }
      }
      sf.polyLine(inner, PAL.outline);
    }
    // plant on top
    const pc = P(0.5, 1.0, h);
    potPlantInto(sf, pc[0], pc[1] + 2, 41, 0.9);
    return { ...base, surface: sf };
  }

  // books along the +y axis on the +x face (for bookcase shelves)
  function booksRowIntoY(sf, P, y0, y1, z, seed) {
    const rng = mulberry32(seed);
    const colors = [PAL.teal, PAL.rose, PAL.sage, PAL.gold, PAL.navy, PAL.cream];
    let y = y0;
    while (y < y1 - 0.06) {
      const bw = 0.11 + rng() * 0.09, bh = 0.28 + rng() * 0.13;
      const color = colors[Math.floor(rng() * colors.length)];
      if (rng() < 0.88) {
        const pts = [P(1, y, z + bh), P(1, y + bw, z + bh), P(1, y + bw, z), P(1, y, z)];
        sf.poly(pts, color);
        sf.line(...P(1, y, z + bh), ...P(1, y, z), shade(color, 0.6));
      }
      y += bw + 0.02;
    }
  }

  // Low bookshelf 2×1 with plant + books on top (like beside the bed).
  function bakeLowShelf() {
    const w = 2, d = 1, h = 0.78;
    const base = bakeBox(w, d, h, PAL.wood, PAL.wood, { topShine: false });
    const sf = base.surface, ox = -base.offset[0], oy = -base.offset[1];
    const P = (x, y, z) => isoPoint(ox, oy, x, y, z);
    // open shelf on the front (+y) face with books
    const inner = [P(0.08, d, h - 0.14), P(w - 0.08, d, h - 0.14), P(w - 0.08, d, 0.1), P(0.08, d, 0.1)];
    sf.poly(inner, shade(PAL.woodDeep, 0.72));
    booksRowInto(sf, (x, y2, z) => P(x, d, z), 0.16, w - 0.5, d, 0.14, 61);
    sf.polyLine(inner, PAL.outline);
    // top: trailing plant + books + clock
    const pc = P(0.4, 0.5, h);
    potPlantInto(sf, pc[0], pc[1] + 2, 71, 0.85);
    const bx = P(1.2, 0.5, h);
    sf.rect(bx[0] - 4, bx[1] - 4, 8, 2, PAL.navy);
    sf.rect(bx[0] - 3, bx[1] - 6, 7, 2, PAL.gold);
    const ck = P(1.7, 0.5, h);
    sf.rect(ck[0] - 2, ck[1] - 6, 5, 6, '#e8dbc2');
    sf.set(ck[0], ck[1] - 4, PAL.outline);
    return { ...base, surface: sf };
  }

  function bakeWardrobe() {
    const w = 1, d = 2, h = 2.05;
    const base = bakeBox(w, d, h, PAL.wood, PAL.wood, { topShine: false });
    const sf = base.surface, ox = -base.offset[0], oy = -base.offset[1];
    const P = (x, y, z) => isoPoint(ox, oy, x, y, z);
    for (let i = 0; i < 2; i += 1) {
      const door = [P(w, i + 0.08, h - 0.1), P(w, i + 0.94, h - 0.1), P(w, i + 0.94, 0.1), P(w, i + 0.08, 0.1)];
      sf.poly(door, shade(PAL.wood, 0.68));
      sf.polyLine(door, shade(PAL.woodDeep, 0.85));
      const inset = [P(w, i + 0.22, h - 0.3), P(w, i + 0.8, h - 0.3), P(w, i + 0.8, 0.32), P(w, i + 0.22, 0.32)];
      sf.polyLine(inset, shade(PAL.wood, 0.52));
      const knob = P(w, i + (i === 0 ? 0.86 : 0.16), h * 0.5);
      sf.rect(knob[0], knob[1], 2, 3, PAL.gold);
    }
    return { ...base, surface: sf };
  }

  function bakeNightstand() {
    const w = 1, d = 1, h = 0.72;
    const base = bakeBox(w, d, h, PAL.wood, PAL.wood, { topShine: false });
    const sf = base.surface, ox = -base.offset[0], oy = -base.offset[1];
    const P = (x, y, z) => isoPoint(ox, oy, x, y, z);
    // drawer on front face
    const face = [P(0.12, d, h - 0.14), P(0.88, d, h - 0.14), P(0.88, d, 0.3), P(0.12, d, 0.3)];
    sf.poly(face, shade(PAL.wood, 0.82));
    sf.polyLine(face, shade(PAL.woodDeep, 0.95));
    const knob = P(0.5, d, (h - 0.1) / 2 + 0.18);
    sf.rect(knob[0] - 1, knob[1], 3, 1, PAL.gold);
    // table lamp: base + warm shade
    const lc = P(0.5, 0.45, h);
    sf.rect(lc[0] - 1, lc[1] - 5, 2, 5, '#8c6b3f');
    sf.poly([[lc[0] - 5, lc[1] - 5], [lc[0] + 5, lc[1] - 5], [lc[0] + 3, lc[1] - 12], [lc[0] - 3, lc[1] - 12]], '#f2cf8e');
    sf.line(lc[0] - 5, lc[1] - 5, lc[0] + 5, lc[1] - 5, shade('#f2cf8e', 0.78));
    sf.polyLine([[lc[0] - 5, lc[1] - 5], [lc[0] + 5, lc[1] - 5], [lc[0] + 3, lc[1] - 12], [lc[0] - 3, lc[1] - 12]], PAL.outline);
    // tiny book
    const bk = P(0.78, 0.7, h);
    sf.rect(bk[0] - 3, bk[1] - 2, 6, 2, PAL.rose);
    return {
      ...base, surface: sf,
      emitters: [{ dx: 0.5, dy: 0.45, z: 0.98, r: 24, color: [255, 205, 130], pool: true }],
    };
  }

  function bakeFloorLamp() {
    const w = 1, d = 1;
    const sf = new Surface(TILE_W + 6, 60);
    const ox = HX + 3, oy = 55;
    const P = (x, y, z) => isoPoint(ox, oy, x, y, z);
    const c = P(0.5, 0.5, 0);
    sf.ellipse(c[0], c[1] - 1, 5, 2.4, PAL.woodDeep);
    sf.ellipse(c[0], c[1] - 2, 5, 2.4, '#6b4a33');
    sf.rect(c[0] - 1, c[1] - 34, 2, 32, '#7c5a40');
    sf.line(c[0] + 1, c[1] - 34, c[0] + 1, c[1] - 2, shade('#7c5a40', 0.7));
    // fabric shade
    sf.poly([[c[0] - 8, c[1] - 34], [c[0] + 8, c[1] - 34], [c[0] + 6, c[1] - 46], [c[0] - 6, c[1] - 46]], '#f2cf8e');
    sf.line(c[0] - 8, c[1] - 34, c[0] + 8, c[1] - 34, shade('#f2cf8e', 0.75));
    sf.line(c[0] - 7, c[1] - 38, c[0] + 7, c[1] - 38, shade('#f2cf8e', 0.9));
    sf.polyLine([[c[0] - 8, c[1] - 34], [c[0] + 8, c[1] - 34], [c[0] + 6, c[1] - 46], [c[0] - 6, c[1] - 46]], PAL.outline);
    return {
      surface: sf, offset: [-ox, -oy], w, d, h: 1.7,
      emitters: [{ dx: 0.5, dy: 0.5, z: 2.5, r: 34, color: [255, 210, 140], pool: true }],
    };
  }

  function bakePendant() {
    // hanging pendant lamp; the cord fades out upward at the implied ceiling
    // height instead of dangling into the void
    const w = 1, d = 1;
    const shadeZ = 1.9;                       // bottom rim of the shade
    const cordTop = 2.85;                     // implied ceiling
    const sf = new Surface(TILE_W + 6, (cordTop + 0.4) * HZ);
    const ox = HX + 3, oy = (cordTop + 0.3) * HZ;
    const P = (x, y, z) => isoPoint(ox, oy, x, y, z);
    const rim = P(0.5, 0.5, shadeZ);
    const top = P(0.5, 0.5, shadeZ + 0.5);
    const cordEnd = P(0.5, 0.5, cordTop);
    sf.line(top[0], top[1], cordEnd[0], cordEnd[1] + 4, '#4a4048');
    // fade the last few cord pixels
    sf.set(cordEnd[0], cordEnd[1] + 3, 'rgba(74,64,72,0.7)');
    sf.set(cordEnd[0], cordEnd[1] + 2, 'rgba(74,64,72,0.45)');
    sf.set(cordEnd[0], cordEnd[1] + 1, 'rgba(74,64,72,0.2)');
    // brass shade: trapezoid with rim highlight
    sf.poly([[rim[0] - 7, rim[1]], [rim[0] + 7, rim[1]], [top[0] + 2, top[1]], [top[0] - 2, top[1]]], PAL.gold);
    sf.line(rim[0] - 7, rim[1], rim[0] + 7, rim[1], '#f4d491');
    sf.line(rim[0] - 6, rim[1] + 1, rim[0] + 6, rim[1] + 1, shade(PAL.gold, 0.72));
    sf.polyLine([[rim[0] - 7, rim[1]], [rim[0] + 7, rim[1]], [top[0] + 2, top[1]], [top[0] - 2, top[1]]], PAL.outline);
    // warm bulb under the shade
    sf.rect(rim[0] - 1, rim[1] + 1, 2, 2, '#ffe9b8');
    return {
      surface: sf, offset: [-ox, -oy], w, d, h: 0,
      decorOccupancy: true,
      emitters: [{ dx: 0.5, dy: 0.5, z: shadeZ - 0.1, r: 40, color: [255, 214, 150], pool: true }],
    };
  }

  function bakePlantBig() {
    // monstera-style plant, 1×1, tall
    const w = 1, d = 1;
    const sf = new Surface(TILE_W + 14, 62);
    const ox = HX + 7, oy = 56;
    const P = (x, y, z) => isoPoint(ox, oy, x, y, z);
    const c = P(0.5, 0.5, 0);
    // ceramic pot
    sf.poly([[c[0] - 7, c[1] - 14], [c[0] + 7, c[1] - 14], [c[0] + 5, c[1] - 2], [c[0] - 5, c[1] - 2]], '#8d9aa4');
    sf.rect(c[0] - 8, c[1] - 16, 16, 3, '#a3b0b8');
    sf.rect(c[0] - 5, c[1] - 3, 10, 1, shade('#8d9aa4', 0.7));
    sf.polyLine([[c[0] - 8, c[1] - 16], [c[0] + 8, c[1] - 16], [c[0] + 5, c[1] - 2], [c[0] - 5, c[1] - 2]], PAL.outline);
    // stems + big split leaves
    const rng = mulberry32(93);
    sf.rect(c[0] - 1, c[1] - 36, 2, 21, '#3f6b40');
    const leafAt = (lx, ly, size, color) => {
      sf.ellipse(lx, ly, size, size * 0.62, color);
      sf.line(lx - size, ly, lx + size, ly, shade(color, 0.72));
      sf.set(lx - size + 1, ly - 1, shade(color, 1.2));
    };
    leafAt(c[0] - 9, c[1] - 36, 6, PAL.leaf);
    leafAt(c[0] + 8, c[1] - 40, 7, PAL.leafLite);
    leafAt(c[0] - 3, c[1] - 46, 6, PAL.leaf);
    leafAt(c[0] + 11, c[1] - 30, 5, PAL.leafDark);
    leafAt(c[0] - 12, c[1] - 26, 5, PAL.leafDark);
    leafAt(c[0] + 2, c[1] - 52, 5, PAL.leafLite);
    // leaf notches
    for (let i = 0; i < 8; i += 1) {
      const lx = c[0] - 12 + rng() * 24, ly = c[1] - 52 + rng() * 24;
      sf.set(lx, ly, 'rgba(20,14,24,0.55)');
    }
    return { surface: sf, offset: [-ox, -oy], w, d, h: 1.7 };
  }

  function bakePlantSmall() {
    const w = 1, d = 1;
    const sf = new Surface(TILE_W + 2, 40);
    const ox = HX + 1, oy = 36;
    const P = (x, y, z) => isoPoint(ox, oy, x, y, z);
    const c = P(0.5, 0.5, 0);
    potPlantInto(sf, c[0], c[1] - 1, 23, 1.15);
    return { surface: sf, offset: [-ox, -oy], w, d, h: 0.9 };
  }

  function bakeStool() {
    const w = 1, d = 1, h = 0.45;
    const { sf, ox, oy, P } = frame(w, d, h * HZ + 4);
    for (const [lx, ly] of [[0.3, 0.32], [0.7, 0.32], [0.3, 0.72], [0.7, 0.72]]) {
      const a = P(lx, ly, h - 0.02), b = P(lx, ly, 0);
      sf.line(a[0], a[1], b[0], b[1], PAL.woodDeep);
    }
    const top = [P(0.2, 0.2, h), P(0.8, 0.2, h), P(0.8, 0.8, h), P(0.2, 0.8, h)];
    sf.poly(top, PAL.teal);
    sf.poly([P(0.2, 0.2, h), P(0.8, 0.2, h), P(0.7, 0.34, h), P(0.3, 0.34, h)], shade(PAL.teal, 1.15));
    sf.polyLine(top, PAL.outline);
    sf.poly([P(0.2, 0.8, h), P(0.8, 0.8, h), P(0.8, 0.8, h - 0.1), P(0.2, 0.8, h - 0.1)], shade(PAL.teal, 0.78));
    return { surface: sf, offset: [-ox, -oy], w, d, h };
  }

  // ---- wall decor -------------------------------------------------------------
  // Wall decor sprites live on a wall plane.  'NE' wall runs along +x at y=0;
  // 'NW' wall runs along +y at x=0.  Bakes return {surface, offset} where the
  // offset anchors the sprite to the projected wall coordinate (t, z) with
  // t measured in tiles along the wall.

  function wallFrame(len, zTop, zBottom, side) {
    const hPx = (zTop - zBottom) * HZ;
    const sf = new Surface(Math.ceil(len * HX) + 4, Math.ceil(len * HY + hPx) + 4);
    // origin at (t=0, z=zTop) top corner; engine anchors sprites via sf.zTop
    const ox = side === 'NE' ? 2 : Math.ceil(len * HX) + 2;
    const oy = 2;
    const dir = side === 'NE' ? 1 : -1;
    sf.zTop = zTop;
    const W = (t, z) => [ox + dir * t * HX, oy + t * HY + (zTop - z) * HZ];
    return { sf, ox, oy, W, hPx };
  }

  function bakeWindowWall(side, mode) {
    // 2-tile window with curtains + city dusk/day view + sill plants
    const len = 2.3;
    const { sf, ox, oy, W } = wallFrame(len, 2.35, 0.55, side);
    const f = { t0: 0.32, t1: 1.98, z0: 0.78, z1: 2.05 };
    // curtain rod
    sf.line(...W(0.02, 2.3), ...W(len - 0.02, 2.3), PAL.woodDeep);
    sf.rect(...W(0.02, 2.3), 1, 3, PAL.woodDeep);
    sf.rect(...W(len - 0.04, 2.3), 1, 3, PAL.woodDeep);
    // window frame
    const framePts = [W(f.t0, f.z1), W(f.t1, f.z1), W(f.t1, f.z0), W(f.t0, f.z0)];
    sf.poly([W(f.t0 - 0.06, f.z1 + 0.06), W(f.t1 + 0.06, f.z1 + 0.06), W(f.t1 + 0.06, f.z0 - 0.06), W(f.t0 - 0.06, f.z0 - 0.06)], '#efe6d2');
    // sky
    if (mode === 'dusk') {
      const bands = ['#2e2a55', '#4a3866', '#7c4a6e', '#b06a6a', '#d99277'];
      for (let i = 0; i < bands.length; i += 1) {
        const zT = f.z1 - (f.z1 - f.z0) * (i / bands.length);
        const zB = f.z1 - (f.z1 - f.z0) * ((i + 1) / bands.length);
        sf.poly([W(f.t0, zT), W(f.t1, zT), W(f.t1, zB), W(f.t0, zB)], bands[i]);
      }
      // city skyline
      const rng = mulberry32(17);
      let t = f.t0 + 0.05;
      while (t < f.t1 - 0.1) {
        const bw = 0.12 + rng() * 0.2, bh = 0.16 + rng() * 0.3;
        const pts = [W(t, f.z0 + bh), W(t + bw, f.z0 + bh), W(t + bw, f.z0), W(t, f.z0)];
        sf.poly(pts, '#241f38');
        // lit windows
        for (let k = 0; k < 3; k += 1) {
          if (rng() < 0.6) {
            const wt = t + 0.03 + rng() * (bw - 0.06), wz = f.z0 + 0.04 + rng() * (bh - 0.08);
            const p = W(wt, wz);
            sf.set(p[0], p[1], rng() < 0.5 ? '#ffd98a' : '#ffb27a');
          }
        }
        t += bw + 0.02;
      }
      // a star or two
      sf.set(...W(f.t0 + 0.3, f.z1 - 0.12), '#f5ecc9');
      sf.set(...W(f.t1 - 0.4, f.z1 - 0.2), '#c9c2e8');
    } else {
      const bands = ['#a8d4e8', '#bce0ee', '#d3ecf4'];
      for (let i = 0; i < bands.length; i += 1) {
        const zT = f.z1 - (f.z1 - f.z0) * (i / bands.length);
        const zB = f.z1 - (f.z1 - f.z0) * ((i + 1) / bands.length);
        sf.poly([W(f.t0, zT), W(f.t1, zT), W(f.t1, zB), W(f.t0, zB)], bands[i]);
      }
      const rng = mulberry32(17);
      let t = f.t0 + 0.05;
      while (t < f.t1 - 0.1) {
        const bw = 0.12 + rng() * 0.2, bh = 0.14 + rng() * 0.26;
        sf.poly([W(t, f.z0 + bh), W(t + bw, f.z0 + bh), W(t + bw, f.z0), W(t, f.z0)], '#8fb4c4');
        t += bw + 0.02;
      }
      // cloud
      const cl = W(f.t0 + 0.5, f.z1 - 0.3);
      sf.rect(cl[0], cl[1], 9, 2, '#fff');
      sf.rect(cl[0] + 2, cl[1] - 2, 5, 2, '#fff');
    }
    // mullions
    const mid = (f.t0 + f.t1) / 2;
    sf.line(...W(mid, f.z1), ...W(mid, f.z0), '#efe6d2');
    sf.line(...W(f.t0, (f.z0 + f.z1) / 2), ...W(f.t1, (f.z0 + f.z1) / 2), '#efe6d2');
    sf.polyLine(framePts, PAL.outline);
    // sill + plants
    sf.poly([W(f.t0 - 0.1, f.z0), W(f.t1 + 0.1, f.z0), W(f.t1 + 0.1, f.z0 - 0.1), W(f.t0 - 0.1, f.z0 - 0.1)], PAL.wallTrim);
    const sp = W(f.t0 + 0.35, f.z0);
    potPlantInto(sf, sp[0], sp[1] + 1, 87, 0.6);
    const sp2 = W(f.t1 - 0.3, f.z0);
    potPlantInto(sf, sp2[0], sp2[1] + 1, 88, 0.55);
    // curtains: gathered panels + slight sway
    const curtain = (t0, t1, seed) => {
      const rng = mulberry32(seed);
      const pts = [W(t0, 2.28), W(t1, 2.28), W(t1 + 0.06, 0.62), W(t0 - 0.06, 0.62)];
      sf.poly(pts, PAL.teal);
      for (let k = 0; k < 4; k += 1) {
        const tt = t0 + (t1 - t0) * (k / 4) + 0.03;
        sf.line(...W(tt, 2.2), ...W(tt + 0.04, 0.7), shade(PAL.teal, k % 2 ? 0.78 : 1.12));
      }
      sf.polyLine(pts, shade(PAL.tealDark, 0.85));
    };
    curtain(0.04, f.t0 - 0.02, 5);
    curtain(f.t1 + 0.02, len - 0.04, 6);
    const glowPts = [];
    return { surface: sf, offset: [-ox, -oy], len, side, glowPts };
  }

  function bakeWallShelf(side) {
    // floating wood shelf with books + trailing plant
    const len = 1.7;
    const { sf, ox, oy, W } = wallFrame(len, 2.5, 1.3, side);
    const z = 1.62;
    // brackets
    for (const t of [0.2, len - 0.2]) {
      const p = W(t, z);
      sf.rect(p[0] - 1, p[1] + 2, 2, 5, PAL.woodDeep);
    }
    // shelf board
    sf.poly([W(0.05, z + 0.05), W(len - 0.05, z + 0.05), W(len - 0.05, z), W(0.05, z)], PAL.wood);
    sf.line(...W(0.05, z), ...W(len - 0.05, z), PAL.woodDeep);
    sf.line(...W(0.05, z + 0.05), ...W(len - 0.05, z + 0.05), PAL.woodLite);
    // books
    const bookP = (t, z2) => W(t, z2);
    booksRowInto(sf, (x, y, z2) => bookP(x, z2), 0.16, len - 0.62, 0, z + 0.05, 45);
    // trailing plant at the end
    const pp = W(len - 0.34, z + 0.05);
    potPlantInto(sf, pp[0], pp[1] - 1, 46, 0.6);
    for (let k = 0; k < 4; k += 1) {
      sf.rect(pp[0] + 3 + k, pp[1] + 2 + k * 3, 2, 3, k % 2 ? PAL.leaf : PAL.leafDark);
    }
    return { surface: sf, offset: [-ox, -oy], len, side };
  }

  function bakePicture(side, seed) {
    const len = 0.62;
    const { sf, ox, oy, W } = wallFrame(len, 2.1, 1.5, side);
    const rng = mulberry32(seed);
    const z0 = 1.55, z1 = 2.02;
    const fr = [W(0.05, z1), W(len - 0.05, z1), W(len - 0.05, z0), W(0.05, z0)];
    sf.poly(fr, PAL.cream);
    sf.polyLine(fr, PAL.woodDeep);
    const inner = [W(0.12, z1 - 0.07), W(len - 0.12, z1 - 0.07), W(len - 0.12, z0 + 0.07), W(0.12, z0 + 0.07)];
    const art = [[PAL.sage, PAL.gold], [PAL.navy, PAL.blush], [PAL.teal, PAL.rose]][Math.floor(rng() * 3)];
    sf.poly(inner, art[0]);
    const c = W(len / 2, (z0 + z1) / 2);
    sf.rect(c[0] - 2, c[1] - 1, 4, 3, art[1]);
    sf.rect(c[0] - 4, c[1] + 2, 8, 1, shade(art[0], 0.75));
    return { surface: sf, offset: [-ox, -oy], len, side };
  }

  function bakePinboard(side) {
    // cork pin board with notes, like above the desk in the reference
    const len = 1.1;
    const { sf, ox, oy, W } = wallFrame(len, 2.05, 1.35, side);
    const z0 = 1.42, z1 = 1.98;
    const fr = [W(0.03, z1), W(len - 0.03, z1), W(len - 0.03, z0), W(0.03, z0)];
    sf.poly(fr, '#c8a878');
    sf.polyLine(fr, PAL.woodDeep);
    const rng = mulberry32(29);
    const noteColors = ['#f6e9a8', '#fdf8ea', PAL.blush, '#cfe3d2'];
    for (let i = 0; i < 6; i += 1) {
      const t = 0.1 + rng() * (len - 0.3), z = z0 + 0.08 + rng() * (z1 - z0 - 0.24);
      const p = W(t, z);
      sf.rect(p[0], p[1], 4, 4, noteColors[Math.floor(rng() * noteColors.length)]);
      sf.set(p[0] + 1, p[1] + 2, 'rgba(40,30,40,0.4)');
      sf.set(p[0] + 2, p[1], PAL.rose);
    }
    // hanging string + light garland below
    return { surface: sf, offset: [-ox, -oy], len, side };
  }

  function bakeStringLights(side, len, seed) {
    // sagging wire with warm bulbs, high on the wall so it clears shelves,
    // cabinets and window frames below
    const { sf, ox, oy, W } = wallFrame(len, 3.06, 2.68, side);
    const zTop = 3.02;
    const glowPts = [];
    const segs = Math.max(2, Math.round(len / 1.1));
    for (let s = 0; s < segs; s += 1) {
      const t0 = (s / segs) * len, t1 = ((s + 1) / segs) * len;
      let prev = W(t0, zTop);
      for (let k = 1; k <= 6; k += 1) {
        const tt = t0 + (t1 - t0) * (k / 6);
        const sag = Math.sin((k / 6) * Math.PI) * 0.1;
        const p = W(tt, zTop - sag);
        sf.line(prev[0], prev[1], p[0], p[1], '#4a4048');
        prev = p;
        if (k % 2 === 0 && k < 6) {
          sf.rect(p[0] - 1, p[1] + 1, 2, 3, '#ffd98a');
          sf.set(p[0] - 1, p[1] + 1, '#fff3cf');
          glowPts.push({ x: p[0] - ox, y: p[1] + 2 - oy, r: 5, color: [255, 214, 140] });
        }
      }
    }
    return { surface: sf, offset: [-ox, -oy], len, side, glowPts };
  }

  function bakeHangingPlant(side) {
    const len = 0.6;
    const { sf, ox, oy, W } = wallFrame(len, 2.85, 1.1, side);
    const c = W(len / 2, 2.77);
    // hook + ropes
    sf.rect(c[0] - 1, c[1], 2, 2, PAL.woodDeep);
    const potY = c[1] + 18;
    sf.line(c[0], c[1] + 2, c[0] - 5, potY, '#8a7a5c');
    sf.line(c[0], c[1] + 2, c[0] + 5, potY, '#8a7a5c');
    // pot
    sf.poly([[c[0] - 6, potY], [c[0] + 6, potY], [c[0] + 4, potY + 6], [c[0] - 4, potY + 6]], PAL.potClay);
    sf.rect(c[0] - 6, potY, 12, 1, shade(PAL.potClay, 1.25));
    // trailing vines
    const rng = mulberry32(side === 'NE' ? 63 : 64);
    for (let v = 0; v < 5; v += 1) {
      let vx = c[0] - 4 + v * 2, vy = potY + 5;
      for (let k = 0; k < 4 + rng() * 4; k += 1) {
        vy += 2 + rng() * 2;
        vx += rng() < 0.5 ? -1 : 1;
        sf.rect(vx, vy, 2, 2, [PAL.leafDark, PAL.leaf, PAL.leafLite][Math.floor(rng() * 3)]);
      }
    }
    return { surface: sf, offset: [-ox, -oy], len, side };
  }

  function bakeUpperCabinet(side) {
    // wall-mounted teal cabinet above the kitchen counter + hanging utensils
    const len = 2.0;
    const { sf, ox, oy, W } = wallFrame(len, 2.6, 1.35, side);
    const z0 = 1.78, z1 = 2.38;
    const body = [W(0.02, z1), W(len - 0.02, z1), W(len - 0.02, z0), W(0.02, z0)];
    sf.poly(body, PAL.teal);
    // underside shadow line
    sf.line(...W(0.02, z0), ...W(len - 0.02, z0), shade(PAL.tealDark, 0.7));
    // doors
    for (let i = 0; i < 2; i += 1) {
      const t0 = 0.08 + i * (len / 2), t1 = (len / 2) - 0.08 + i * (len / 2);
      const door = [W(t0, z1 - 0.06), W(t1, z1 - 0.06), W(t1, z0 + 0.06), W(t0, z0 + 0.06)];
      sf.polyLine(door, shade(PAL.tealDark, 0.9));
      const knob = W(i === 0 ? t1 - 0.08 : t0 + 0.08, (z0 + z1) / 2);
      sf.rect(knob[0], knob[1], 2, 2, PAL.gold);
    }
    sf.polyLine(body, PAL.outline);
    // small plant + jar on top
    const pp = W(0.4, z1);
    potPlantInto(sf, pp[0], pp[1], 99, 0.55);
    const jar = W(1.4, z1);
    sf.rect(jar[0] - 2, jar[1] - 5, 4, 5, PAL.gold);
    sf.rect(jar[0] - 2, jar[1] - 6, 4, 1, PAL.woodDeep);
    // hanging utensil rail below
    sf.line(...W(0.2, z0 - 0.12), ...W(len - 0.2, z0 - 0.12), '#8f9ba2');
    for (const [t, kind] of [[0.5, 'spoon'], [0.9, 'pan'], [1.4, 'cup']]) {
      const p = W(t, z0 - 0.12);
      sf.line(p[0], p[1], p[0], p[1] + 3, '#8f9ba2');
      if (kind === 'pan') { sf.ellipse(p[0], p[1] + 6, 3, 2.4, '#3a3a44'); }
      else if (kind === 'spoon') { sf.rect(p[0] - 1, p[1] + 3, 1, 5, '#c9b18a'); sf.rect(p[0] - 2, p[1] + 8, 3, 2, '#c9b18a'); }
      else { sf.rect(p[0] - 2, p[1] + 4, 4, 4, PAL.rose); }
    }
    return { surface: sf, offset: [-ox, -oy], len, side };
  }

  function bakeBacksplash(side, len) {
    // tile pattern behind the counter, from counter top up to cabinets
    const { sf, ox, oy, W } = wallFrame(len, 1.8, 0.9, side);
    const z0 = 0.95, z1 = 1.75;
    const body = [W(0, z1), W(len, z1), W(len, z0), W(0, z0)];
    sf.poly(body, '#ded2b8');
    for (let zz = z0; zz < z1; zz += 0.2) sf.line(...W(0, zz), ...W(len, zz), '#c9bb9e');
    for (let tt = 0; tt < len; tt += 0.25) sf.line(...W(tt, z0), ...W(tt, z1), '#c9bb9e');
    return { surface: sf, offset: [-ox, -oy], len, side };
  }

  function bakeWallClock(side) {
    const len = 0.5;
    const { sf, ox, oy, W } = wallFrame(len, 2.5, 1.75, side);
    const c = W(len / 2, 2.05);
    sf.ellipse(c[0], c[1], 5.4, 5.4, PAL.cream);
    sf.ellipse(c[0], c[1], 5.4, 5.4, PAL.cream);
    sf.set(c[0], c[1] - 4, PAL.outline); sf.set(c[0], c[1] + 4, PAL.outline);
    sf.set(c[0] - 4, c[1], PAL.outline); sf.set(c[0] + 4, c[1], PAL.outline);
    sf.line(c[0], c[1], c[0], c[1] - 3, PAL.outline);
    sf.line(c[0], c[1], c[0] + 2, c[1] + 1, PAL.roseDark);
    // rim
    for (const [dx, dy] of [[-5, -2], [5, -2], [-5, 2], [5, 2], [0, -5], [0, 5]]) sf.set(c[0] + dx, c[1] + dy, PAL.woodDeep);
    return { surface: sf, offset: [-ox, -oy], len, side };
  }

  // ---- door ------------------------------------------------------------------

  function bakeDoor(side) {
    const len = 1.0;
    const { sf, ox, oy, W } = wallFrame(len, 2.2, 0, side);
    const frame_ = [W(0.02, 2.12), W(len - 0.02, 2.12), W(len - 0.02, 0), W(0.02, 0)];
    sf.poly(frame_, shade(PAL.wood, 0.7));
    const panel = [W(0.09, 2.04), W(len - 0.09, 2.04), W(len - 0.09, 0), W(0.09, 0)];
    sf.poly(panel, PAL.wood);
    sf.poly([W(0.09, 2.04), W(len - 0.09, 2.04), W(len - 0.09, 1.98), W(0.09, 1.98)], PAL.woodLite);
    for (const [zz0, zz1] of [[1.2, 1.85], [0.25, 1.0]]) {
      const inset = [W(0.22, zz1), W(len - 0.22, zz1), W(len - 0.22, zz0), W(0.22, zz0)];
      sf.polyLine(inset, shade(PAL.woodDeep, 0.95));
    }
    const knob = W(len - 0.2, 1.05);
    sf.rect(knob[0], knob[1], 2, 2, PAL.gold);
    sf.polyLine(frame_, PAL.outline);
    return { surface: sf, offset: [-ox, -oy], len, side };
  }

  return {
    bakeBox, bakeFloorTile, bakeRug,
    bakeBed, bakeSofa, bakeCoffeeTable, bakeChair, bakeDeskSet, bakeDiningSet,
    bakeKitchen, bakeFridge, bakeBookcase, bakeLowShelf, bakeWardrobe,
    bakeNightstand, bakeFloorLamp, bakePendant, bakePlantBig, bakePlantSmall, bakeStool,
    bakeWindowWall, bakeWallShelf, bakePicture, bakePinboard, bakeStringLights,
    bakeHangingPlant, bakeUpperCabinet, bakeBacksplash, bakeWallClock, bakeDoor,
  };
})();
