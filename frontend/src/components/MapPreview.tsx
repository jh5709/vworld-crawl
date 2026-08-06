import { useState, useEffect, useCallback, useRef } from "react";
import DeckGL from "@deck.gl/react";
import {
  GeoJsonLayer,
  PolygonLayer,
  ScatterplotLayer,
  BitmapLayer,
} from "@deck.gl/layers";
import { TileLayer } from "@deck.gl/geo-layers";
import {
  X, Maximize2, Minimize2, Table2, BoxSelect,
  BarChart3, Loader2, AlertTriangle,
  ChevronLeft, ChevronRight, ChevronDown,
} from "lucide-react";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

interface Bounds {
  xmin: number;
  ymin: number;
  xmax: number;
  ymax: number;
}

interface ColumnStat {
  name: string;
  type: string;
  count: number | null;
  non_null: number | null;
  nulls: number | null;
  distinct: number | null;
  min: string | null;
  max: string | null;
  avg: number | null;
  std: number | null;
  q25: number | null;
  q50: number | null;
  q75: number | null;
}

interface HistogramBin {
  label: string;
  count: number;
}

interface Histogram {
  column: string;
  kind: "numeric" | "categorical";
  total: number;
  distinct: number;
  bins?: HistogramBin[];
  categories?: HistogramBin[];
  error?: string;
}

interface FeatureCollection {
  type: "FeatureCollection";
  features: any[];
  total_matching: number;
  returned: number;
  bounded: boolean;
  geometry_type: string;
  limit: number;
}

interface AttributePage {
  columns: string[];
  rows: Record<string, unknown>[];
  total: number;
  offset: number;
  limit: number;
}

// ---------------------------------------------------------------------------
// Bright palette for dark basemap
// ---------------------------------------------------------------------------

function geoColor(geomType: string): [number, number, number] {
  switch (geomType) {
    case "POINT": return [34, 211, 238];       // cyan-400
    case "LINESTRING": return [232, 121, 249]; // fuchsia-400
    case "POLYGON": return [163, 230, 53];     // lime-400
    default: return [226, 232, 240];            // slate-200
  }
}

// ---------------------------------------------------------------------------
// Main Component
// ---------------------------------------------------------------------------

interface MapPreviewProps {
  tableName: string;
  onClose: () => void;
}

export default function MapPreview({ tableName, onClose }: MapPreviewProps) {
  // --- State ---
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [bounds, setBounds] = useState<Bounds | null>(null);
  const [stats, setStats] = useState<ColumnStat[]>([]);
  const [features, setFeatures] = useState<FeatureCollection | null>(null);
  const [attributes, setAttributes] = useState<AttributePage | null>(null);
  const [featuresLoading, setFeaturesLoading] = useState(false);
  const [attrsLoading, setAttrsLoading] = useState(false);

  // --- Selection box state ---
  const [selBox, setSelBox] = useState<Bounds | null>(null);
  const [drawMode, setDrawMode] = useState(false);
  const [drawing, setDrawing] = useState(false);
  const [dragStart, setDragStart] = useState<[number, number] | null>(null);
  const [dragEnd, setDragEnd] = useState<[number, number] | null>(null);

  // --- View state ---
  const [viewState, setViewState] = useState({
    longitude: 127.0,
    latitude: 36.0,
    zoom: 6,
  });

  // --- Side panel ---
  const [showPanel, setShowPanel] = useState<"stats" | "attrs">("stats");
  const [attrPage, setAttrPage] = useState(0);
  const ATTR_PAGE_SIZE = 50;

  // --- Histograms (per expanded column) ---
  const [expandedCol, setExpandedCol] = useState<string | null>(null);
  const [histograms, setHistograms] = useState<Record<string, Histogram>>({});
  const [histLoading, setHistLoading] = useState<string | null>(null);

  const deckRef = useRef<any>(null);

  // --- Initial load: bounds + stats ---
  useEffect(() => {
    (async () => {
      setLoading(true);
      setError(null);
      try {
        const [bRes, sRes] = await Promise.all([
          fetch(`/api/tables/${encodeURIComponent(tableName)}/bounds`),
          fetch(`/api/tables/${encodeURIComponent(tableName)}/stats`),
        ]);
        const bData = await bRes.json();
        const sData = await sRes.json();

        if (bData.error) { setError(bData.error); return; }
        setBounds({ xmin: bData.xmin, ymin: bData.ymin, xmax: bData.xmax, ymax: bData.ymax });
        setStats(sData.columns || []);

        // Fit view to bounds
        const cx = (bData.xmin + bData.xmax) / 2;
        const cy = (bData.ymin + bData.ymax) / 2;
        const span = Math.max(bData.xmax - bData.xmin, bData.ymax - bData.ymin, 0.01);
        const zoom = Math.max(2, Math.min(16, Math.floor(Math.log2(360 / span))));
        setViewState({ longitude: cx, latitude: cy, zoom: zoom + 1 });
      } catch (e: any) {
        setError(e.message);
      } finally {
        setLoading(false);
      }
    })();
  }, [tableName]);

  // --- Load features when selection changes ---
  const loadFeatures = useCallback(async (box: Bounds | null) => {
    setFeaturesLoading(true);
    try {
      const params = new URLSearchParams();
      if (box) {
        params.set("xmin", String(box.xmin));
        params.set("ymin", String(box.ymin));
        params.set("xmax", String(box.xmax));
        params.set("ymax", String(box.ymax));
      }
      const res = await fetch(
        `/api/tables/${encodeURIComponent(tableName)}/features?${params}`,
      );
      const data = await res.json();
      if (data.error) setError(data.error);
      else setFeatures(data);
    } catch (e: any) {
      console.error(e);
    } finally {
      setFeaturesLoading(false);
    }
  }, [tableName]);

  // --- Load attributes for table view ---
  const loadAttributes = useCallback(async (box: Bounds | null, page: number) => {
    setAttrsLoading(true);
    try {
      const body: any = { offset: page * ATTR_PAGE_SIZE, limit: ATTR_PAGE_SIZE };
      if (box) {
        body.xmin = box.xmin; body.ymin = box.ymin;
        body.xmax = box.xmax; body.ymax = box.ymax;
      }
      const res = await fetch(
        `/api/tables/${encodeURIComponent(tableName)}/attributes`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        },
      );
      setAttributes(await res.json());
    } catch (e: any) {
      console.error(e);
    } finally {
      setAttrsLoading(false);
    }
  }, [tableName]);

  // --- Load histogram for a column (lazy, cached) ---
  const toggleHistogram = useCallback(async (col: string) => {
    if (expandedCol === col) {
      setExpandedCol(null);
      return;
    }
    setExpandedCol(col);
    if (histograms[col]) return; // cached
    setHistLoading(col);
    try {
      const res = await fetch(
        `/api/tables/${encodeURIComponent(tableName)}/histogram`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ column: col }),
        },
      );
      const data = await res.json();
      setHistograms((prev) => ({ ...prev, [col]: data }));
    } catch (e: any) {
      setHistograms((prev) => ({
        ...prev,
        [col]: { column: col, kind: "categorical", total: 0, distinct: 0, error: e.message },
      }));
    } finally {
      setHistLoading(null);
    }
  }, [expandedCol, histograms, tableName]);

  // --- Pixel to coordinate conversion ---
  const pixelToCoord = useCallback((pixelX: number, pixelY: number) => {
    if (!deckRef.current) return null;
    const viewport = deckRef.current.deck?.getViewports()?.[0];
    if (!viewport) return null;
    const coords = viewport.unproject([pixelX, pixelY]);
    return { lng: coords[0], lat: coords[1] };
  }, []);

  // --- Rectangle draw handlers (only active in draw mode) ---
  const onDragStart = useCallback((info: any) => {
    if (!drawMode || !info.pixel) return;
    setDrawing(true);
    setDragStart(info.pixel);
    setDragEnd(info.pixel);
  }, [drawMode]);

  const onDrag = useCallback((info: any) => {
    if (!drawing || !info.pixel) return;
    setDragEnd(info.pixel);
  }, [drawing]);

  const onDragEnd = useCallback(() => {
    if (!dragStart || !dragEnd) return;
    setDrawing(false);
    const start = pixelToCoord(dragStart[0], dragStart[1]);
    const end = pixelToCoord(dragEnd[0], dragEnd[1]);
    setDragStart(null);
    setDragEnd(null);
    if (!start || !end) return;
    const box: Bounds = {
      xmin: Math.min(start.lng, end.lng),
      ymin: Math.min(start.lat, end.lat),
      xmax: Math.max(start.lng, end.lng),
      ymax: Math.max(start.lat, end.lat),
    };
    setSelBox(box);
    setDrawMode(false);
    loadFeatures(box);
    loadAttributes(box, 0);
    setAttrPage(0);
  }, [dragStart, dragEnd, pixelToCoord, loadFeatures, loadAttributes]);

  const clearSelection = useCallback(() => {
    setSelBox(null);
    setFeatures(null);
    setAttributes(null);
    loadFeatures(null);
    loadAttributes(null, 0);
    setAttrPage(0);
  }, [loadFeatures, loadAttributes]);

  // --- DeckGL layers ---
  const layers: any[] = [];

  // Base map: CARTO dark tiles
  layers.push(
    new TileLayer({
      id: "carto-dark",
      data: "https://basemaps.cartocdn.com/dark_all/{z}/{x}/{y}.png",
      minZoom: 0,
      maxZoom: 19,
      tileSize: 256,
      renderSubLayers: (props: any) => {
        const { boundingBox } = props.tile;
        return new BitmapLayer(props, {
          data: undefined,
          image: props.data,
          bounds: [boundingBox[0][0], boundingBox[0][1], boundingBox[1][0], boundingBox[1][1]],
        });
      },
    }),
  );

  // Selection box (confirmed)
  if (selBox) {
    layers.push(
      new PolygonLayer({
        id: "selection-box",
        data: [[
          [selBox.xmin, selBox.ymin],
          [selBox.xmax, selBox.ymin],
          [selBox.xmax, selBox.ymax],
          [selBox.xmin, selBox.ymax],
        ]],
        getPolygon: (d: any) => d,
        getFillColor: [34, 211, 238, 25],
        getLineColor: [34, 211, 238, 220],
        getLineWidth: 2,
        lineWidthUnits: "pixels" as const,
      }),
    );
  }

  // Drawing preview (while dragging)
  if (drawing && dragStart && dragEnd) {
    const s = pixelToCoord(dragStart[0], dragStart[1]);
    const e = pixelToCoord(dragEnd[0], dragEnd[1]);
    if (s && e) {
      const box: any = [[
        [Math.min(s.lng, e.lng), Math.min(s.lat, e.lat)],
        [Math.max(s.lng, e.lng), Math.min(s.lat, e.lat)],
        [Math.max(s.lng, e.lng), Math.max(s.lat, e.lat)],
        [Math.min(s.lng, e.lng), Math.max(s.lat, e.lat)],
      ]];
      layers.push(
        new PolygonLayer({
          id: "drawing-box",
          data: box,
          getPolygon: (d: any) => d,
          getFillColor: [34, 211, 238, 15],
          getLineColor: [34, 211, 238, 180],
          getLineWidth: 1,
          lineWidthUnits: "pixels" as const,
          getDashArray: [4, 4] as any,
        }),
      );
    }
  }

  // Full-table bounding box outline
  if (bounds) {
    layers.push(
      new PolygonLayer({
        id: "table-bounds",
        data: [[
          [bounds.xmin, bounds.ymin],
          [bounds.xmax, bounds.ymin],
          [bounds.xmax, bounds.ymax],
          [bounds.xmin, bounds.ymax],
        ]],
        getPolygon: (d: any) => d,
        getFillColor: [0, 0, 0, 0],
        getLineColor: [250, 204, 21, 200],
        getLineWidth: 2,
        lineWidthUnits: "pixels" as const,
        getDashArray: [8, 4] as any,
      }),
    );
  }

  // Features layer — bright colors on dark basemap
  if (features && features.features.length > 0) {
    const gtype = features.geometry_type;
    const [r, g, b] = geoColor(gtype);

    if (gtype === "POINT") {
      layers.push(
        new ScatterplotLayer({
          id: "features-points",
          data: features.features,
          getPosition: (d: any) => d.geometry?.coordinates || [0, 0],
          getRadius: 6,
          radiusUnits: "pixels" as const,
          getFillColor: [r, g, b, 230],
          getLineColor: [255, 255, 255, 120],
          getLineWidth: 1,
          stroked: true,
          pickable: true,
          autoHighlight: true,
        }),
      );
    } else {
      layers.push(
        new GeoJsonLayer({
          id: "features-geo",
          data: features.features,
          filled: gtype === "POLYGON",
          stroked: true,
          getFillColor: [r, g, b, 90],
          getLineColor: [r, g, b, 240],
          getLineWidth: gtype === "LINESTRING" ? 2 : 1.5,
          lineWidthUnits: "pixels" as const,
          pointRadiusUnits: "pixels" as const,
          pointRadiusMinPixels: 3,
          pickable: true,
          autoHighlight: true,
        }),
      );
    }
  }

  // --- Escape key to close ---
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [onClose]);

  if (loading) {
    return (
      <div className="fixed inset-0 bg-black/60 z-50 flex items-center justify-center">
        <div className="bg-neutral-900 border border-neutral-700 rounded-xl p-8 flex items-center gap-3">
          <Loader2 className="w-5 h-5 animate-spin text-neutral-400" />
          <span className="text-sm text-neutral-300">Loading map data...</span>
        </div>
      </div>
    );
  }

  if (error) {
    return (
      <div className="fixed inset-0 bg-black/60 z-50 flex items-center justify-center">
        <div className="bg-neutral-900 border border-neutral-700 rounded-xl p-6 space-y-3 max-w-sm">
          <div className="flex items-start gap-3">
            <AlertTriangle className="w-5 h-5 text-amber-400 shrink-0 mt-0.5" />
            <div>
              <p className="text-sm text-neutral-200">Cannot load map</p>
              <p className="text-xs text-neutral-500 mt-1">{error}</p>
            </div>
          </div>
          <button
            onClick={onClose}
            className="w-full px-3 py-1.5 text-xs bg-neutral-700 text-neutral-300 rounded-lg hover:bg-neutral-600"
          >
            Close
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className="fixed inset-0 bg-black/85 z-50 flex">
      {/* Map area */}
      <div className="flex-1 relative">
        <DeckGL
          ref={deckRef}
          viewState={viewState}
          onViewStateChange={({ viewState: vs }: any) => setViewState(vs)}
          controller={{
            dragPan: !drawMode,
            dragRotate: false,
            keyboard: true,
            doubleClickZoom: true,
          }}
          layers={layers}
          getCursor={() => drawMode ? "crosshair" : "grab"}
          onDragStart={onDragStart}
          onDrag={onDrag}
          onDragEnd={onDragEnd}
        />

        {/* Info bar */}
        <div className="absolute top-3 left-3 flex flex-col gap-2">
          <div className="bg-neutral-900/90 border border-neutral-700/50 rounded-lg px-3 py-1.5 text-xs text-neutral-300 space-y-0.5">
            <div className="flex items-center gap-2">
              <span className="font-mono text-neutral-200">{tableName}</span>
            </div>
            {features && (
              <div className="text-neutral-400">
                {features.total_matching.toLocaleString()} rows
                {features.bounded && (
                  <span> • showing {features.returned.toLocaleString()}</span>
                )}
                <span className="text-neutral-500 ml-1">
                  ({features.geometry_type}, limit {features.limit.toLocaleString()})
                </span>
              </div>
            )}
          </div>
        </div>

        {/* Top-right controls */}
        <div className="absolute top-3 right-3 flex gap-1">
          <button
            onClick={() => setDrawMode((v) => !v)}
            className={`px-2.5 py-1.5 text-xs border rounded-lg transition-colors ${
              drawMode
                ? "bg-cyan-500/20 border-cyan-400/50 text-cyan-300"
                : "bg-neutral-800/90 border-neutral-700/50 text-neutral-300 hover:bg-neutral-700/90"
            }`}
            title={drawMode ? "Drawing: drag a rectangle on the map" : "Select area"}
          >
            <BoxSelect className="w-3.5 h-3.5" />
          </button>
          {selBox && (
            <button
              onClick={clearSelection}
              className="px-2.5 py-1.5 text-xs bg-neutral-800/90 border border-neutral-700/50 text-neutral-300 rounded-lg hover:bg-neutral-700/90 transition-colors"
              title="Clear selection — show full table"
            >
              <Minimize2 className="w-3.5 h-3.5" />
            </button>
          )}
          {bounds && (
            <button
              onClick={() => {
                const cx = (bounds.xmin + bounds.xmax) / 2;
                const cy = (bounds.ymin + bounds.ymax) / 2;
                const span = Math.max(
                  bounds.xmax - bounds.xmin,
                  bounds.ymax - bounds.ymin,
                  0.01,
                );
                const zoom = Math.max(2, Math.min(16, Math.floor(Math.log2(360 / span))));
                setViewState({ longitude: cx, latitude: cy, zoom: zoom + 1 });
              }}
              className="px-2.5 py-1.5 text-xs bg-neutral-800/90 border border-neutral-700/50 text-neutral-300 rounded-lg hover:bg-neutral-700/90 transition-colors"
              title="Fit to table"
            >
              <Maximize2 className="w-3.5 h-3.5" />
            </button>
          )}
          <button
            onClick={onClose}
            className="px-2.5 py-1.5 text-xs bg-neutral-800/90 border border-neutral-700/50 text-neutral-400 rounded-lg hover:bg-neutral-700/90 hover:text-neutral-200 transition-colors"
          >
            <X className="w-3.5 h-3.5" />
          </button>
        </div>

        {/* Loading spinner for features */}
        {featuresLoading && (
          <div className="absolute bottom-3 left-3 bg-neutral-900/90 border border-neutral-700/50 rounded-lg px-3 py-1.5 flex items-center gap-2 text-xs text-neutral-400">
            <Loader2 className="w-3 h-3 animate-spin" />
            Loading features...
          </div>
        )}
      </div>

      {/* Side panel */}
      <div className="w-96 border-l border-neutral-700 bg-neutral-900 flex flex-col">
        {/* Tab bar */}
        <div className="flex border-b border-neutral-800">
          <button
            onClick={() => setShowPanel("stats")}
            className={`flex-1 flex items-center justify-center gap-1.5 py-2 text-xs font-medium transition-colors ${
              showPanel === "stats"
                ? "text-emerald-400 border-b border-emerald-400 bg-emerald-400/5"
                : "text-neutral-500 hover:text-neutral-300"
            }`}
          >
            <BarChart3 className="w-3 h-3" /> Statistics
          </button>
          <button
            onClick={() => {
              setShowPanel("attrs");
              if (!attributes) loadAttributes(selBox, 0);
            }}
            className={`flex-1 flex items-center justify-center gap-1.5 py-2 text-xs font-medium transition-colors ${
              showPanel === "attrs"
                ? "text-cyan-400 border-b border-cyan-400 bg-cyan-400/5"
                : "text-neutral-500 hover:text-neutral-300"
            }`}
          >
            <Table2 className="w-3 h-3" /> Attributes
          </button>
        </div>

        {/* Panel content */}
        <div className="flex-1 overflow-y-auto">
          {showPanel === "stats" && (
            <div className="p-3">
              {stats.length === 0 ? (
                <p className="text-xs text-neutral-600">No statistics available</p>
              ) : (
                <div className="space-y-0.5">
                  {stats.map((col) => {
                    const expanded = expandedCol === col.name;
                    const hist = histograms[col.name];
                    return (
                      <div key={col.name} className="bg-neutral-800/50 rounded">
                        <button
                          onClick={() => toggleHistogram(col.name)}
                          className="w-full text-left px-2.5 py-1.5 text-[11px] hover:bg-neutral-800 rounded transition-colors"
                        >
                          <div className="flex items-center justify-between mb-0.5">
                            <span className="text-neutral-200 font-mono flex items-center gap-1">
                              <ChevronDown
                                className={`w-3 h-3 text-neutral-600 transition-transform ${
                                  expanded ? "" : "-rotate-90"
                                }`}
                              />
                              {col.name}
                            </span>
                            <span className="text-neutral-600">{col.type}</span>
                          </div>
                          <div className="grid grid-cols-3 gap-x-2 text-neutral-500">
                            <span>
                              {col.count != null ? col.count.toLocaleString() : "—"} rows
                            </span>
                            <span>
                              {col.distinct != null ? col.distinct.toLocaleString() : "—"} distinct
                            </span>
                            <span>
                              {col.nulls != null ? col.nulls.toLocaleString() : "—"} nulls
                            </span>
                          </div>
                          {col.avg != null && (
                            <div className="text-neutral-500 mt-0.5">
                              avg {col.avg.toPrecision(4)}
                              {col.q50 != null && (
                                <span className="text-neutral-600">
                                  {" "}• median {col.q50.toPrecision(4)}
                                </span>
                              )}
                            </div>
                          )}
                          {col.avg == null && (col.min || col.max) && (
                            <div className="text-neutral-600 mt-0.5 truncate">
                              {col.min} … {col.max}
                            </div>
                          )}
                        </button>

                        {/* Histogram (expanded) */}
                        {expanded && (
                          <div className="px-2.5 pb-2 pt-1">
                            {histLoading === col.name ? (
                              <div className="flex items-center gap-2 text-[11px] text-neutral-600 py-2">
                                <Loader2 className="w-3 h-3 animate-spin" /> Loading…
                              </div>
                            ) : hist?.error ? (
                              <p className="text-[11px] text-red-400">{hist.error}</p>
                            ) : hist?.kind === "numeric" && hist.bins ? (
                              <NumericHistogram bins={hist.bins} />
                            ) : hist?.kind === "categorical" && hist.categories ? (
                              <CategoricalHistogram categories={hist.categories} />
                            ) : null}
                          </div>
                        )}
                      </div>
                    );
                  })}
                </div>
              )}
            </div>
          )}

          {showPanel === "attrs" && (
            <div>
              {attrsLoading ? (
                <div className="flex items-center gap-2 p-4 text-xs text-neutral-500">
                  <Loader2 className="w-3 h-3 animate-spin" />
                  Loading attributes...
                </div>
              ) : attributes ? (
                <div>
                  <div className="text-[11px] text-neutral-600 px-3 py-2 border-b border-neutral-800">
                    {attributes.total.toLocaleString()} rows
                    {attributes.columns.length > 0 && (
                      <span> • {attributes.columns.length} columns</span>
                    )}
                    {selBox && <span> • filtered</span>}
                  </div>
                  <div className="overflow-x-auto">
                    <table className="w-full text-[11px]">
                      <thead>
                        <tr className="border-b border-neutral-800 bg-neutral-900 sticky top-0">
                          {attributes.columns.map((c) => (
                            <th
                              key={c}
                              className="px-2 py-1.5 text-left font-medium text-neutral-500 font-mono whitespace-nowrap max-w-[120px] truncate"
                            >
                              {c}
                            </th>
                          ))}
                        </tr>
                      </thead>
                      <tbody className="divide-y divide-neutral-800/30">
                        {attributes.rows.map((row, i) => (
                          <tr key={i} className="hover:bg-neutral-800/50">
                            {attributes.columns.map((c) => (
                              <td
                                key={c}
                                className="px-2 py-0.5 text-neutral-400 font-mono whitespace-nowrap max-w-[120px] truncate"
                              >
                                {String(row[c] ?? "")}
                              </td>
                            ))}
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>

                  {/* Pagination */}
                  {attributes.total > ATTR_PAGE_SIZE && (
                    <div className="flex items-center justify-between px-3 py-2 border-t border-neutral-800 text-[11px]">
                      <span className="text-neutral-600">
                        {attrPage * ATTR_PAGE_SIZE + 1}–{Math.min(
                          (attrPage + 1) * ATTR_PAGE_SIZE,
                          attributes.total,
                        )}{" "}
                        of {attributes.total.toLocaleString()}
                      </span>
                      <div className="flex gap-1">
                        <button
                          onClick={() => {
                            const p = Math.max(0, attrPage - 1);
                            setAttrPage(p);
                            loadAttributes(selBox, p);
                          }}
                          disabled={attrPage === 0}
                          className="px-2 py-0.5 text-neutral-500 hover:text-neutral-300 disabled:opacity-30"
                        >
                          <ChevronLeft className="w-3 h-3" />
                        </button>
                        <button
                          onClick={() => {
                            const p = attrPage + 1;
                            setAttrPage(p);
                            loadAttributes(selBox, p);
                          }}
                          disabled={(attrPage + 1) * ATTR_PAGE_SIZE >= attributes.total}
                          className="px-2 py-0.5 text-neutral-500 hover:text-neutral-300 disabled:opacity-30"
                        >
                          <ChevronRight className="w-3 h-3" />
                        </button>
                      </div>
                    </div>
                  )}
                </div>
              ) : (
                <p className="p-4 text-xs text-neutral-600">
                  Select an area on the map to load attributes.
                </p>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Histogram renderers (pure CSS bars — no chart library)
// ---------------------------------------------------------------------------

function NumericHistogram({ bins }: { bins: HistogramBin[] }) {
  const max = Math.max(...bins.map((b) => b.count), 1);
  return (
    <div>
      <div className="flex items-end gap-px h-16">
        {bins.map((b, i) => (
          <div
            key={i}
            className="flex-1 bg-emerald-500/70 hover:bg-emerald-400 rounded-sm transition-colors"
            style={{ height: `${Math.max((b.count / max) * 100, b.count > 0 ? 4 : 0)}%` }}
            title={`${b.label}: ${b.count.toLocaleString()}`}
          />
        ))}
      </div>
      <div className="flex justify-between text-[10px] text-neutral-600 mt-1">
        <span>{bins[0]?.label.split("–")[0]}</span>
        <span>{bins[bins.length - 1]?.label.split("–")[1]}</span>
      </div>
    </div>
  );
}

function CategoricalHistogram({ categories }: { categories: HistogramBin[] }) {
  const max = Math.max(...categories.map((c) => c.count), 1);
  return (
    <div className="space-y-0.5">
      {categories.map((c, i) => (
        <div key={i} className="flex items-center gap-1.5 text-[10px]">
          <span className="w-24 truncate text-neutral-400" title={c.label}>
            {c.label}
          </span>
          <div className="flex-1 h-3 bg-neutral-800 rounded-sm overflow-hidden">
            <div
              className="h-full bg-cyan-500/70 rounded-sm"
              style={{ width: `${(c.count / max) * 100}%` }}
            />
          </div>
          <span className="w-12 text-right text-neutral-500">
            {c.count.toLocaleString()}
          </span>
        </div>
      ))}
    </div>
  );
}
