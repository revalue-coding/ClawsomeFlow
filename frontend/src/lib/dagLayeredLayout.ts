/**
 * Sugiyama-lite layered DAG layout shared by the Flow editor dependency
 * graph and the Run task board.
 *
 * Pipeline:
 *   1. longest-path depth → columns (summary pinned to the rightmost column)
 *   2. barycenter ordering within columns (a few LTR/RTL passes)
 *   3. preferred column/row gaps, compressed only when the graph is dense
 *      AND within ``DAG_HSCROLL_COL_THRESHOLD`` columns (wider graphs keep
 *      preferred gaps; the UI scrolls horizontally instead of shrinking)
 *   4. content block centered inside max(minCanvas, naturalSize) — never
 *      stretch sparse graphs across empty canvas, never upscale node size
 *      (unless ``minDisplayCols`` expands the canvas for a fixed UI scale)
 */

/** Past this many columns, layout stops compressing gaps; the graph UI
 *  should scroll horizontally at the scale of a 5-column fit. */
export const DAG_HSCROLL_COL_THRESHOLD = 5;

export interface DagLayoutInputNode {
  id: string;
  dependsOn: string[];
  /** Pin this node to its own rightmost column (leader summary). */
  isSummary?: boolean;
}

export interface DagLayoutOptions {
  minWidth?: number;
  minHeight?: number;
  padX?: number;
  padY?: number;
  /** Soft ceiling before column gaps compress toward colGapMin. */
  maxWidth?: number;
  /** Soft ceiling before row gaps compress toward rowGapMin. */
  maxHeight?: number;
  colGapPref?: number;
  colGapMin?: number;
  colGapMax?: number;
  rowGapPref?: number;
  rowGapMin?: number;
  rowGapMax?: number;
  /** Worker-node radius clamp (summary is sized by the caller). */
  nodeRadiusMin?: number;
  nodeRadiusMax?: number;
  /**
   * When the graph has fewer depth columns than this, expand the canvas width
   * (and ``fitWidth``) as if it had this many columns so the UI can lock
   * on-screen node scale to a denser reference layout.
   */
  minDisplayCols?: number;
}

export interface DagLaidOutNode {
  id: string;
  x: number;
  y: number;
  depth: number;
}

export interface DagLayoutResult {
  nodes: DagLaidOutNode[];
  positions: Map<string, DagLaidOutNode>;
  width: number;
  height: number;
  /** Base worker radius derived from gaps; hard-capped so sparse graphs stay calm. */
  suggestedNodeRadius: number;
  maxDepth: number;
  /** Number of depth columns (including the summary column). */
  colCount: number;
  /**
   * ViewBox width of the same layout clipped to
   * ``DAG_HSCROLL_COL_THRESHOLD`` columns. The UI sizes the SVG so this
   * width maps to 100% of the viewport; wider graphs overflow and scroll.
   */
  fitWidth: number;
}

const DEFAULTS = {
  minWidth: 320,
  minHeight: 280,
  padX: 56,
  padY: 48,
  maxWidth: 1100,
  maxHeight: 720,
  colGapPref: 108,
  colGapMin: 64,
  colGapMax: 128,
  rowGapPref: 68,
  rowGapMin: 44,
  rowGapMax: 86,
  nodeRadiusMin: 7,
  nodeRadiusMax: 11,
} as const;

function clamp(n: number, lo: number, hi: number): number {
  return Math.max(lo, Math.min(hi, n));
}

function median(values: number[]): number | null {
  if (values.length === 0) return null;
  const sorted = [...values].sort((a, b) => a - b);
  const mid = Math.floor(sorted.length / 2);
  return sorted.length % 2 === 0
    ? (sorted[mid - 1] + sorted[mid]) / 2
    : sorted[mid];
}

/** Longest-path depth from roots; cycles collapse to 0 for the looping edge. */
export function dagLongestPathDepths(
  nodes: DagLayoutInputNode[],
): Map<string, number> {
  const byId = new Map(nodes.map((n) => [n.id, n] as const));
  const depth = new Map<string, number>();
  const visiting = new Set<string>();

  function depthOf(id: string): number {
    const hit = depth.get(id);
    if (hit != null) return hit;
    if (visiting.has(id)) return 0;
    visiting.add(id);
    const node = byId.get(id);
    const deps = (node?.dependsOn ?? []).filter((d) => byId.has(d));
    const d = deps.length === 0 ? 0 : 1 + Math.max(...deps.map(depthOf));
    visiting.delete(id);
    depth.set(id, d);
    return d;
  }

  for (const n of nodes) depthOf(n.id);
  return depth;
}

/**
 * Layered layout. Coordinates are in SVG viewBox space; the caller keeps
 * edge geometry and visual chrome (palette, glyphs, hover).
 */
export function layoutDagLayered(
  input: DagLayoutInputNode[],
  options: DagLayoutOptions = {},
): DagLayoutResult {
  const cfg = { ...DEFAULTS, ...options };
  const usable = input.filter((n) => n.id.trim());
  if (usable.length === 0) {
    return {
      nodes: [],
      positions: new Map(),
      width: cfg.minWidth,
      height: cfg.minHeight,
      suggestedNodeRadius: cfg.nodeRadiusMin,
      maxDepth: 0,
      colCount: 0,
      fitWidth: cfg.minWidth,
    };
  }

  const byId = new Map(usable.map((n) => [n.id, n] as const));
  const rawDepth = dagLongestPathDepths(usable);

  let maxWorkerDepth = 0;
  for (const n of usable) {
    if (!n.isSummary) {
      maxWorkerDepth = Math.max(maxWorkerDepth, rawDepth.get(n.id) ?? 0);
    }
  }
  // Summary always occupies its own rightmost column, one step past workers.
  const summaryDepth = maxWorkerDepth + 1;
  const depth = new Map<string, number>();
  for (const n of usable) {
    depth.set(n.id, n.isSummary ? summaryDepth : (rawDepth.get(n.id) ?? 0));
  }
  const maxDepth = Math.max(0, ...depth.values());
  const colCount = maxDepth + 1;

  // Adjacency for barycenter passes.
  const outs = new Map<string, string[]>();
  const ins = new Map<string, string[]>();
  for (const n of usable) {
    outs.set(n.id, []);
    ins.set(n.id, []);
  }
  for (const n of usable) {
    for (const dep of n.dependsOn) {
      if (!byId.has(dep)) continue;
      outs.get(dep)!.push(n.id);
      ins.get(n.id)!.push(dep);
    }
  }

  // Columns keyed by depth; initial order is stable by id.
  const columns: string[][] = Array.from({ length: colCount }, () => []);
  const sortedIds = [...usable.map((n) => n.id)].sort((a, b) => a.localeCompare(b));
  for (const id of sortedIds) {
    columns[depth.get(id)!].push(id);
  }

  // Temporary y in "row index" space for barycenter; refined after each pass.
  const yIndex = new Map<string, number>();
  function assignEvenIndices() {
    for (const col of columns) {
      col.forEach((id, i) => yIndex.set(id, i));
    }
  }
  assignEvenIndices();

  function reorderColumn(
    colIdx: number,
    neighborYs: (id: string) => number[],
  ) {
    const col = columns[colIdx];
    if (col.length <= 1) return;
    const scored = col.map((id, idx) => {
      const bary = median(neighborYs(id));
      return { id, score: bary ?? yIndex.get(id) ?? idx };
    });
    scored.sort((a, b) => {
      if (a.score !== b.score) return a.score - b.score;
      return a.id.localeCompare(b.id);
    });
    columns[colIdx] = scored.map((s) => s.id);
  }

  // A few left→right / right→left barycenter sweeps — enough for typical
  // Flow sizes (≤30) without pulling in a layout library.
  for (let pass = 0; pass < 3; pass += 1) {
    for (let c = 1; c < colCount; c += 1) {
      reorderColumn(c, (id) =>
        (ins.get(id) ?? []).map((u) => yIndex.get(u) ?? 0),
      );
    }
    assignEvenIndices();
    for (let c = colCount - 2; c >= 0; c -= 1) {
      reorderColumn(c, (id) =>
        (outs.get(id) ?? []).map((d) => yIndex.get(d) ?? 0),
      );
    }
    assignEvenIndices();
  }

  const maxColSize = Math.max(1, ...columns.map((c) => c.length));

  const minDisplayCols = Math.max(0, cfg.minDisplayCols ?? 0);
  /** Column count used to derive horizontal gap (may exceed actual ``colCount``). */
  const gapLayoutCols = Math.max(colCount, minDisplayCols);

  // Preferred gaps. Horizontal compression only applies while the graph
  // still fits in the scroll threshold — beyond that we keep preferred
  // spacing and let the UI scroll rather than shrink nodes/edges.
  let gapX = cfg.colGapPref;
  let gapY = cfg.rowGapPref;
  if (gapLayoutCols > 1) {
    const naturalW = cfg.padX * 2 + (gapLayoutCols - 1) * gapX;
    if (
      gapLayoutCols <= DAG_HSCROLL_COL_THRESHOLD
      && naturalW > cfg.maxWidth
    ) {
      gapX = Math.max(
        cfg.colGapMin,
        (cfg.maxWidth - cfg.padX * 2) / (gapLayoutCols - 1),
      );
    }
    gapX = clamp(gapX, cfg.colGapMin, cfg.colGapMax);
  } else {
    gapX = 0;
  }
  if (maxColSize > 1) {
    const naturalH = cfg.padY * 2 + (maxColSize - 1) * gapY;
    if (naturalH > cfg.maxHeight) {
      gapY = Math.max(cfg.rowGapMin, (cfg.maxHeight - cfg.padY * 2) / (maxColSize - 1));
    }
    gapY = clamp(gapY, cfg.rowGapMin, cfg.rowGapMax);
  } else {
    gapY = 0;
  }

  const blockW = colCount > 1 ? (colCount - 1) * gapX : 0;
  const blockH = maxColSize > 1 ? (maxColSize - 1) * gapY : 0;
  const displayColCount = gapLayoutCols;
  const displayBlockW =
    displayColCount > 1 ? (displayColCount - 1) * gapX : 0;
  const width = Math.max(cfg.minWidth, displayBlockW + cfg.padX * 2);
  const height = Math.max(cfg.minHeight, blockH + cfg.padY * 2);
  const originX = cfg.padX + (width - cfg.padX * 2 - blockW) / 2;
  const originY = cfg.padY + (height - cfg.padY * 2 - blockH) / 2;

  const nodes: DagLaidOutNode[] = [];
  for (let c = 0; c < colCount; c += 1) {
    const col = columns[c];
    const colBlockH = col.length > 1 ? (col.length - 1) * gapY : 0;
    // Center short columns inside the overall block so branches don't hug the top.
    const colOriginY = originY + (blockH - colBlockH) / 2;
    col.forEach((id, i) => {
      const x = colCount === 1 ? width / 2 : originX + c * gapX;
      const y = col.length === 1 ? originY + blockH / 2 : colOriginY + i * gapY;
      nodes.push({ id, x, y, depth: c });
    });
  }

  const positions = new Map(nodes.map((n) => [n.id, n] as const));

  // Radius follows the tighter gap but is hard-capped — few-node graphs
  // keep comfortable spacing without ballooning the dots.
  const refGap = Math.min(
    gapLayoutCols > 1 ? gapX : cfg.colGapPref,
    maxColSize > 1 ? gapY : cfg.rowGapPref,
  );
  const suggestedNodeRadius = clamp(
    refGap * 0.155,
    cfg.nodeRadiusMin,
    cfg.nodeRadiusMax,
  );

  const fitCols = Math.min(gapLayoutCols, DAG_HSCROLL_COL_THRESHOLD);
  const fitBlockW = fitCols > 1 ? (fitCols - 1) * gapX : 0;
  const fitWidth = Math.max(cfg.minWidth, fitBlockW + cfg.padX * 2);

  return {
    nodes,
    positions,
    width,
    height,
    suggestedNodeRadius,
    maxDepth,
    colCount,
    fitWidth,
  };
}
