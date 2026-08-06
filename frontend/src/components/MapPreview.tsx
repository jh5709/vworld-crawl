import { useState, useEffect, useCallback, useRef } from "react";
import DeckGL from "@deck.gl/react";
import {
  GeoJsonLayer,
  PolygonLayer,
  ScatterplotLayer,
} from "@deck.gl/layers";
import { TileLayer } from "@deck.gl/geo-layers";
import { BitmapLayer } from "@deck.gl/layers";
import {
  X, Maximize2, Minimize2, Table2,
  BarChart3, Loader2, AlertTriangle,
  ChevronLeft, ChevronRight,
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
// Color ramp for polygon fill
// ---------------------------------------------------------------------------

function geoColor(geomType: string): [number, number, number] {
  switch (geomType) {
    case "POINT": return [59, 130, 246];      // blue
    case "LINESTRING": return [168, 85, 247];  // purple
    case "POLYGON": return [34, 197, 94];       // green
    default: return [148, 163, 184];             // gray
  }
}

// ---------------------------------------------------------------------------
// Helpers
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
      const body: any = {};
      if (box) {
        body.xmin = box.xmin; body.ymin = box.ymin;
        body.xmax = box.xmax; body.ymax = box.ymax;
      }
      const res = await fetch(
        `/api/tables/${encodeURIComponent(tableName)}/features`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        },
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

  // --- Pixel to coordinate conversion ---
  const pixelToCoord = useCallback((pixelX: number, pixelY: number) => {
    if (!deckRef.current) return null;
    const viewport = deckRef.current.deck?.getViewports()?.[0];
    if (!viewport) return null;
    const coords = viewport.unproject([pixelX, pixelY]);
    return { lng: coords[0], lat: coords[1] };
  }, []);

  // --- Handle map pointer events for rectangle draw ---
  const onDragStart = useCallback((_info: any) => {
    if (!_info.pixel) return;
    setDrawing(true);
    setDragStart(_info.pixel);
    setDragEnd(_info.pixel);
  }, []);

  const onDrag = useCallback((info: any) => {
    if (!drawing || !info.pixel) return;
    setDragEnd(info.pixel);
  }, [drawing]);

  const onDragEnd = useCallback((_info: any) => {
    if (!dragStart || !dragEnd) return;
    setDrawing(false);
    const start = pixelToCoord(dragStart[0], dragStart[1]);
    const end = pixelToCoord(dragEnd[0], dragEnd[1]);
    if (!start || !end) return;
    const box: Bounds = {
      xmin: Math.min(start.lng, end.lng),
      ymin: Math.min(start.lat, end.lat),
      xmax: Math.max(start.lng, end.lng),
      ymax: Math.max(start.lat, end.lat),
    };
    // Ignore tiny drags (< 5px)
    const dx = dragEnd[0] - dragStart[0];
    const dy = dragEnd[1] - dragStart[1];
    if (Math.abs(dx) < 5 && Math.abs(dy) < 5) {
      setSelBox(null);
      setFeatures(null);
      setAttributes(null);
      loadFeatures(null);
      loadAttributes(null, 0);
      return;
    }
    setSelBox(box);
    loadFeatures(box);
    loadAttributes(box, 0);
    setAttrPage(0);
    setDragStart(null);
    setDragEnd(null);
  }, [dragStart, dragEnd, pixelToCoord, loadFeatures, loadAttributes]);

  // --- DeckGL layers ---
  const layers: any[] = [];

  // Base map: OSM raster tiles
  layers.push(
    new TileLayer({
      id: "osm-tiles",
      data: "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
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

  // Selection box (while drawing or confirmed)
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
        getFillColor: [59, 130, 246, 30],
        getLineColor: [59, 130, 246, 180],
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
          getFillColor: [255, 255, 255, 20],
          getLineColor: [255, 255, 255, 140],
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
        getLineColor: [255, 193, 7, 160],
        getLineWidth: 2,
        lineWidthUnits: "pixels" as const,
        getDashArray: [8, 4] as any,
      }),
    );
  }

  // Features layer
  if (features && features.features.length > 0) {
    const gtype = features.geometry_type;
    const [r, g, b] = geoColor(gtype);

    if (gtype === "POINT") {
      layers.push(
        new ScatterplotLayer({
          id: "features-points",
          data: features.features,
          getPosition: (d: any) => d.geometry?.coordinates || [0, 0],
          getRadius: 80,
          radiusUnits: "pixels",
          getFillColor: [r, g, b, 180],
          getLineColor: [r, g, b, 220],
          getLineWidth: 1,
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
          getFillColor: [r, g, b, 60],
          getLineColor: [r, g, b, 200],
          getLineWidth: gtype === "LINESTRING" ? 2 : 1,
          lineWidthUnits: "pixels",
          pointRadiusUnits: "pixels",
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
            dragRotate: false,
            keyboard: true,
            doubleClickZoom: true,
          }}
          layers={layers}
          getCursor={() => drawing ? "crosshair" : "grab"}
          onDragStart={onDragStart}
          onDrag={onDrag}
          onDragEnd={onDragEnd}
        />

        {/* Map overlay controls */}
        <div className="absolute top-3 left-3 flex flex-col gap-2">
          {/* Info bar */}
          <div className="bg-neutral-900/90 border border-neutral-700/50 rounded-lg px-3 py-1.5 text-xs text-neutral-300 space-y-0.5">
            <div className="flex items-center gap-2">
              <span className="font-mono text-neutral-200">{tableName}</span>
            </div>
            {features && (
              <div className="text-neutral-500">
                {features.total_matching.toLocaleString()} rows total
                {features.bounded && (
                  <span> • showing {features.returned.toLocaleString()}</span>
                )}
                <span className="text-neutral-600 ml-1">
                  ({features.geometry_type}, limit {features.limit.toLocaleString()})
                </span>
              </div>
            )}
          </div>

          {/* Draw instruction */}
          {!selBox && (
            <div className="bg-neutral-900/90 border border-neutral-700/50 rounded-lg px-2.5 py-1 text-[11px] text-neutral-500">
              Drag to select an area
            </div>
          )}
        </div>

        {/* Top-right buttons */}
        <div className="absolute top-3 right-3 flex gap-1">
          <button
            onClick={() => {
              if (selBox) {
                // Deselect — show full table
                setSelBox(null);
                setFeatures(null);
                setAttributes(null);
                loadFeatures(null);
                loadAttributes(null, 0);
              } else if (bounds) {
                // Fit to full table
                const cx = (bounds.xmin + bounds.xmax) / 2;
                const cy = (bounds.ymin + bounds.ymax) / 2;
                const span = Math.max(
                  bounds.xmax - bounds.xmin,
                  bounds.ymax - bounds.ymin,
                  0.01,
                );
                const zoom = Math.max(2, Math.min(16, Math.floor(Math.log2(360 / span))));
                setViewState({ longitude: cx, latitude: cy, zoom: zoom + 1 });
              }
            }}
            className="px-2.5 py-1.5 text-xs bg-neutral-800/90 border border-neutral-700/50 text-neutral-300 rounded-lg hover:bg-neutral-700/90 transition-colors"
            title={selBox ? "Clear selection" : "Fit to table"}
          >
            {selBox ? <Minimize2 className="w-3.5 h-3.5" /> : <Maximize2 className="w-3.5 h-3.5" />}
          </button>
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
                ? "text-blue-400 border-b border-blue-400 bg-blue-400/5"
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
                  {stats.map((col) => (
                    <div
                      key={col.name}
                      className="bg-neutral-800/50 rounded px-2.5 py-1.5 text-[11px]"
                    >
                      <div className="flex items-center justify-between mb-0.5">
                        <span className="text-neutral-200 font-mono">{col.name}</span>
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
                      {(col.min || col.max) && (
                        <div className="text-neutral-600 mt-0.5 truncate">
                          {col.min} … {col.max}
                        </div>
                      )}
                    </div>
                  ))}
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
