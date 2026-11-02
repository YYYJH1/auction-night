// Arrows between the boxes of a figure.
//
// A figure lays its boxes out with plain CSS. This file measures them and draws the arrows in an
// <svg class="overlay"> that covers the card, so a box can change size without the arrows being
// redrawn by hand. Every arrow ends in a small filled head that points the way the flow goes.
//
//   connect(card, svg, [
//     ['right', 'a', 'b'],              from the right edge of a to the left edge of b
//     ['down',  'a', 'b'],              from the bottom of a to the top of b (an elbow when they are not aligned)
//     ['down',  'a', 'b', {label: 'x', labelWidth: 120}],
//     ['left',  'a', 'b'],              from the left edge of a to the right edge of b
//     ['up',    'a', 'b'],              from the top of a to the bottom of b
//   ])
//
// Each box is found by its id. Options: {fromX, toX} are fractions (0..1) of the box width at
// which a vertical arrow leaves and enters (default 0.5), {midY} is the height of the horizontal
// part of an elbow, {gap} is the distance between a box and the end of its arrow, {dash: true}
// draws a dashed line and {accent: true} uses the accent colour.

(() => {
  const NS = 'http://www.w3.org/2000/svg';
  const HEAD = 7; // half the width of an arrow head, in pixels
  const GAP = 4; // distance between a box and the end of its arrow

  function box(card, id) {
    const el = document.getElementById(id);
    if (el === null) throw new Error(`no box with id ${id}`);
    const r = el.getBoundingClientRect();
    const c = card.getBoundingClientRect();
    return { x: r.left - c.left, y: r.top - c.top, w: r.width, h: r.height };
  }

  function add(svg, tag, attrs) {
    const node = document.createElementNS(NS, tag);
    for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, String(value));
    svg.appendChild(node);
    return node;
  }

  /** A filled triangle whose tip is at (x, y) and that points in `dir`. */
  function head(svg, x, y, dir, accent) {
    const l = HEAD * 1.5;
    const w = HEAD * 0.85;
    const pts = {
      right: [[x, y], [x - l, y - w], [x - l, y + w]],
      left: [[x, y], [x + l, y - w], [x + l, y + w]],
      down: [[x, y], [x - w, y - l], [x + w, y - l]],
      up: [[x, y], [x - w, y + l], [x + w, y + l]],
    }[dir];
    add(svg, 'polygon', {
      points: pts.map((p) => p.join(',')).join(' '),
      class: accent ? 'fill-accent' : 'fill-line',
    });
  }

  function line(svg, d, { dash = false, accent = false } = {}) {
    const node = add(svg, 'path', { d, class: accent ? 'stroke-accent' : 'stroke-line' });
    if (dash) node.setAttribute('stroke-dasharray', '5 5');
  }

  function label(svg, x, y, width, text) {
    const holder = add(svg, 'foreignObject', { x: x - width / 2, y: y - 18, width, height: 36 });
    const div = document.createElement('div');
    div.className = 'arrow-label';
    div.innerHTML = text;
    holder.appendChild(div);
  }

  /** A polyline through the given points (card coordinates) with a head at the last one. */
  function poly(svg, points, { dash = false, accent = false } = {}) {
    const [tipX, tipY] = points[points.length - 1];
    const [prevX, prevY] = points[points.length - 2];
    const dir = Math.abs(tipX - prevX) > Math.abs(tipY - prevY) ? (tipX > prevX ? 'right' : 'left') : tipY > prevY ? 'down' : 'up';
    const back = HEAD * 1.5 - 1;
    const end = { right: [tipX - back, tipY], left: [tipX + back, tipY], down: [tipX, tipY - back], up: [tipX, tipY + back] }[dir];
    const path = points.slice(0, -1).map(([x, y], i) => `${i === 0 ? 'M' : 'L'} ${x} ${y}`).join(' ');
    line(svg, `${path} L ${end[0]} ${end[1]}`, { dash, accent });
    head(svg, tipX, tipY, dir, accent);
  }

  window.connect = (card, svg, arrows) => {
    for (const [kind, fromId, toId, options = {}] of arrows) {
      if (kind === 'poly') {
        poly(svg, options.points, options);
        if (options.label) label(svg, options.labelX, options.labelY, options.labelWidth ?? 120, options.label);
        continue;
      }
      const a = box(card, fromId);
      const b = box(card, toId);
      const { fromX = 0.5, toX = 0.5, dash = false, accent = false, gap = GAP, dy = 0 } = options;
      const style = { dash, accent };
      if (kind === 'right' || kind === 'left') {
        const dir = kind;
        const y = a.y + a.h / 2 + dy;
        const x1 = dir === 'right' ? a.x + a.w + gap : a.x - gap;
        const x2 = dir === 'right' ? b.x - gap : b.x + b.w + gap;
        const tail = dir === 'right' ? x2 - HEAD * 1.5 + 1 : x2 + HEAD * 1.5 - 1;
        line(svg, `M ${x1} ${y} H ${tail}`, style);
        head(svg, x2, y, dir, accent);
        if (options.label) label(svg, (x1 + x2) / 2, y - 14, options.labelWidth ?? 80, options.label);
      } else {
        const dir = kind;
        const down = dir === 'down';
        const x1 = a.x + a.w * fromX;
        const x2 = b.x + b.w * toX;
        const y1 = down ? a.y + a.h + gap : a.y - gap;
        const y2 = down ? b.y - gap : b.y + b.h + gap;
        const tail = down ? y2 - HEAD * 1.5 + 1 : y2 + HEAD * 1.5 - 1;
        if (Math.abs(x1 - x2) < 1) {
          line(svg, `M ${x1} ${y1} V ${tail}`, style);
        } else {
          const mid = options.midY ?? (y1 + y2) / 2;
          line(svg, `M ${x1} ${y1} V ${mid} H ${x2} V ${tail}`, style);
        }
        head(svg, x2, y2, dir, accent);
        if (options.label) {
          const lx = options.labelX ?? (x1 + x2) / 2;
          const ly = options.labelY ?? (y1 + y2) / 2;
          label(svg, lx, ly, options.labelWidth ?? 120, options.label);
        }
      }
    }
  };
})();
