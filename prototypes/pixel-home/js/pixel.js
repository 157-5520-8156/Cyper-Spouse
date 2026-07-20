'use strict';

// ---------------------------------------------------------------------------
// Pixel drawing kit: crisp software rasterization onto small offscreen
// canvases.  Everything is baked at native pixel resolution (1 tile =
// 32x16 px) and later blitted with nearest-neighbour scaling.
// ---------------------------------------------------------------------------

const TILE_W = 32, TILE_H = 16;
const HX = TILE_W / 2, HY = TILE_H / 2;   // half tile in px
const HZ = 16;                            // 1 unit of height in px

// --- color helpers ---------------------------------------------------------

function hexToRgb(hex) {
  return [parseInt(hex.slice(1, 3), 16), parseInt(hex.slice(3, 5), 16), parseInt(hex.slice(5, 7), 16)];
}

function rgbToHex(r, g, b) {
  const c = v => Math.max(0, Math.min(255, Math.round(v))).toString(16).padStart(2, '0');
  return `#${c(r)}${c(g)}${c(b)}`;
}

// factor < 1 darkens, > 1 lightens (screen-ish blend toward white)
function shade(hex, factor) {
  const [r, g, b] = hexToRgb(hex);
  if (factor <= 1) return rgbToHex(r * factor, g * factor, b * factor);
  const t = factor - 1;
  return rgbToHex(r + (255 - r) * t, g + (255 - g) * t, b + (255 - b) * t);
}

function mulberry32(seed) {
  let a = seed >>> 0;
  return function () {
    a |= 0; a = (a + 0x6D2B79F5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

// --- shared palette --------------------------------------------------------

const PAL = {
  outline:   '#2a1c2e',
  // floors
  floorA:    '#b07d4e',
  floorB:    '#a5714a',
  floorSeam: '#7e5233',
  floorLite: '#c99a64',
  kitchenA:  '#c9a87c',
  kitchenB:  '#bd9a70',
  // walls
  wall:      '#e3d3b4',
  wallLow:   '#cdb894',
  wallTrim:  '#9c7350',
  // woods
  wood:      '#96603a',
  woodDark:  '#6d4128',
  woodLite:  '#b97f4e',
  woodDeep:  '#54301f',
  // fabrics & accents
  cream:     '#efe3c8',
  linen:     '#f0e6cf',
  teal:      '#4e8d80',
  tealDark:  '#38685f',
  sage:      '#7ea183',
  sageDark:  '#5c7a62',
  rose:      '#c47a6d',
  roseDark:  '#9c5751',
  blush:     '#e0a793',
  gold:      '#e3b263',
  navy:      '#4a5f82',
  // plants
  leaf:      '#4c8552',
  leafLite:  '#6fae67',
  leafDark:  '#33633f',
  potClay:   '#a9603f',
  // character
  skin:      '#f6d3ae',
  skinDark:  '#dda57e',
  hair:      '#8a5638',
  hairLite:  '#ab7449',
  hairDark:  '#663d26'
};

// --- Surface ---------------------------------------------------------------

class Surface {
  constructor(w, h) {
    this.w = w; this.h = h;
    this.canvas = document.createElement('canvas');
    this.canvas.width = w; this.canvas.height = h;
    this.ctx = this.canvas.getContext('2d');
    this.ctx.imageSmoothingEnabled = false;
  }

  set(x, y, color) {
    this.ctx.fillStyle = color;
    this.ctx.fillRect(Math.round(x), Math.round(y), 1, 1);
  }

  rect(x, y, w, h, color) {
    this.ctx.fillStyle = color;
    this.ctx.fillRect(Math.round(x), Math.round(y), Math.round(w), Math.round(h));
  }

  line(x0, y0, x1, y1, color) {
    x0 = Math.round(x0); y0 = Math.round(y0); x1 = Math.round(x1); y1 = Math.round(y1);
    const dx = Math.abs(x1 - x0), dy = -Math.abs(y1 - y0);
    const sx = x0 < x1 ? 1 : -1, sy = y0 < y1 ? 1 : -1;
    let err = dx + dy;
    for (;;) {
      this.set(x0, y0, color);
      if (x0 === x1 && y0 === y1) break;
      const e2 = 2 * err;
      if (e2 >= dy) { err += dy; x0 += sx; }
      if (e2 <= dx) { err += dx; y0 += sy; }
    }
  }

  // even-odd scanline fill, sampled at pixel centers -> crisp diamond edges
  poly(pts, color) {
    let minY = Infinity, maxY = -Infinity;
    for (const [, y] of pts) { minY = Math.min(minY, y); maxY = Math.max(maxY, y); }
    for (let y = Math.floor(minY); y <= Math.ceil(maxY); y += 1) {
      const yc = y + 0.5, xs = [];
      for (let i = 0; i < pts.length; i += 1) {
        const [x1, y1] = pts[i], [x2, y2] = pts[(i + 1) % pts.length];
        if ((y1 <= yc && y2 > yc) || (y2 <= yc && y1 > yc)) xs.push(x1 + (yc - y1) * (x2 - x1) / (y2 - y1));
      }
      xs.sort((a, b) => a - b);
      for (let k = 0; k + 1 < xs.length; k += 2) {
        const x0 = Math.ceil(xs[k] - 0.5), x1 = Math.floor(xs[k + 1] - 0.5);
        if (x1 >= x0) this.rect(x0, y, x1 - x0 + 1, 1, color);
      }
    }
  }

  polyLine(pts, color) {
    for (let i = 0; i < pts.length; i += 1) {
      const [x1, y1] = pts[i], [x2, y2] = pts[(i + 1) % pts.length];
      this.line(x1, y1, x2, y2, color);
    }
  }

  // crisp scanline-filled pixel ellipse
  ellipse(cx, cy, rx, ry, color) {
    for (let y = Math.floor(cy - ry); y <= Math.ceil(cy + ry); y += 1) {
      const t = (y + 0.5 - cy) / ry;
      if (Math.abs(t) > 1) continue;
      const half = rx * Math.sqrt(1 - t * t);
      const x0 = Math.ceil(cx - half - 0.5), x1 = Math.floor(cx + half - 0.5);
      if (x1 >= x0) this.rect(x0, y, x1 - x0 + 1, 1, color);
    }
  }

  dither(pts, color, density, rng) {
    let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
    for (const [x, y] of pts) {
      minX = Math.min(minX, x); maxX = Math.max(maxX, x);
      minY = Math.min(minY, y); maxY = Math.max(maxY, y);
    }
    for (let y = Math.floor(minY); y <= Math.ceil(maxY); y += 1) {
      for (let x = Math.floor(minX); x <= Math.ceil(maxX); x += 1) {
        if (rng() < density && insidePoly(pts, x + 0.5, y + 0.5)) this.set(x, y, color);
      }
    }
  }

  blit(surface, x, y) {
    this.ctx.drawImage(surface.canvas, Math.round(x), Math.round(y));
  }

  mirrored() {
    const out = new Surface(this.w, this.h);
    out.ctx.translate(this.w, 0);
    out.ctx.scale(-1, 1);
    out.ctx.drawImage(this.canvas, 0, 0);
    out.ctx.setTransform(1, 0, 0, 1, 0, 0);
    return out;
  }

  static fromMap(rows, legend) {
    const h = rows.length, w = Math.max(...rows.map(r => r.length));
    const sf = new Surface(w, h);
    for (let y = 0; y < h; y += 1) {
      for (let x = 0; x < rows[y].length; x += 1) {
        const color = legend[rows[y][x]];
        if (color) sf.set(x, y, color);
      }
    }
    return sf;
  }
}

function insidePoly(pts, x, y) {
  let inside = false;
  for (let i = 0, j = pts.length - 1; i < pts.length; j = i, i += 1) {
    const [xi, yi] = pts[i], [xj, yj] = pts[j];
    if ((yi > y) !== (yj > y) && x < ((xj - xi) * (y - yi)) / (yj - yi) + xi) inside = !inside;
  }
  return inside;
}

// Local isometric projection inside a sprite surface.
// (ax, ay) is the pixel of grid point (0,0) at z=0.
function isoPoint(ax, ay, x, y, z) {
  return [ax + (x - y) * HX, ay + (x + y) * HY - z * HZ];
}

function isoQuad(ax, ay, corners) {
  return corners.map(([x, y, z]) => isoPoint(ax, ay, x, y, z));
}
