import { useState, useEffect } from "react";
import {
  Database, RefreshCw, HardDrive, Trash2, Clock,
  AlertTriangle, ChevronRight, Eye, Info,
  Download, FileArchive,
} from "lucide-react";

interface CrawlSourceFile {
  url: string;
  file_name: string;
  etag: string;
  last_modified: string;
  file_size: number;
  local_path: string;
  status: string;
  downloaded_at: string;
}

interface StagedFile {
  url: string;
  file_name: string;
  file_size: number;
  local_path: string;
  downloaded_at: string;
}

interface StagedSummary {
  total_files: number;
  total_size: number;
  not_loaded: StagedFile[];
}

interface TableInfo {
  name: string;
  rows: number;
  file_count: number;
  total_size: number;
  is_reject: boolean;
  latest_snapshot: number | null;
  last_modified: string | null;
}

interface Snapshot {
  snapshot_id: number;
  timestamp: string;
  change: string;
}

interface OpResult {
  success: boolean;
  message?: string;
  error?: string;
  unsupported?: boolean;
}

interface ExpirePreview {
  snapshots_expired: number;
  snapshot_ids: number[];
}

interface PreviewData {
  columns: string[];
  rows: Record<string, unknown>[];
  total_rows: number;
}

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function formatDate(ts: string | null): string {
  if (!ts) return "—";
  try {
    return new Date(ts).toLocaleString();
  } catch {
    return ts;
  }
}

export default function DuckLakeConsole({
  onLoadToPipeline,
}: {
  onLoadToPipeline?: (filePath: string) => void;
}) {
  const [tables, setTables] = useState<TableInfo[]>([]);
  const [loading, setLoading] = useState(true);
  const [selectedTable, setSelectedTable] = useState<string | null>(null);
  const [snapshots, setSnapshots] = useState<Snapshot[]>([]);
  const [snapLoading, setSnapLoading] = useState(false);
  const [opResult, setOpResult] = useState<OpResult | null>(null);
  const [showExpire, setShowExpire] = useState(false);
  const [days, setDays] = useState(30);
  const [expirePreview, setExpirePreview] = useState<ExpirePreview | null>(null);
  const [preview, setPreview] = useState<PreviewData | null>(null);

  // --- Source files (crawl_state linkage) ---
  const [sourceFiles, setSourceFiles] = useState<CrawlSourceFile[]>([]);
  const [sourceLoading, setSourceLoading] = useState(false);
  const [sourceError, setSourceError] = useState<string | null>(null);
  const [cleaningUp, setCleaningUp] = useState<string | null>(null);
  const [redownloading, setRedownloading] = useState<string | null>(null);

  // --- Download staging (originals on disk) ---
  const [staged, setStaged] = useState<StagedSummary | null>(null);
  const [deletingStaged, setDeletingStaged] = useState(false);
  const [confirmDeleteAll, setConfirmDeleteAll] = useState(false);

  const fetchStaged = async () => {
    try {
      const res = await fetch("/api/crawler/staged");
      if (!res.ok) return; // older backend without staging endpoints
      setStaged(await res.json());
    } catch {
      /* backend unreachable — staging section stays hidden */
    }
  };

  const fetchTables = async () => {
    setLoading(true);
    try {
      const res = await fetch("/api/tables");
      const data = await res.json();
      setTables(data.tables || []);
    } catch (e) {
      console.error(e);
    } finally {
      setLoading(false);
    }
    fetchStaged();
  };

  useEffect(() => { fetchTables(); }, []);

  const fetchSnapshots = async (name: string) => {
    if (selectedTable === name) {
      setSelectedTable(null);
      setSourceFiles([]);
      return;
    }
    setSelectedTable(name);
    setSnapLoading(true);
    setSourceLoading(true);
    setSourceError(null);
    setOpResult(null);
    // Fetch snapshots and sources independently — a sources failure
    // (e.g. stale backend without the endpoint) must not hide snapshots.
    try {
      const snapRes = await fetch(`/api/tables/${encodeURIComponent(name)}/snapshots`);
      const snapData = await snapRes.json();
      setSnapshots(snapData.snapshots || []);
    } catch (e) {
      console.error(e);
    } finally {
      setSnapLoading(false);
    }
    try {
      const srcRes = await fetch(`/api/tables/${encodeURIComponent(name)}/sources`);
      if (!srcRes.ok) throw new Error(`HTTP ${srcRes.status} — restart the backend?`);
      const srcData = await srcRes.json();
      setSourceFiles(srcData.sources || []);
    } catch (e: any) {
      console.error(e);
      setSourceFiles([]);
      setSourceError(e.message || "Failed to load source files");
    } finally {
      setSourceLoading(false);
    }
  };

  const handleDeleteStaged = async (urls: string[]) => {
    setDeletingStaged(true);
    setOpResult(null);
    try {
      const res = await fetch("/api/crawler/staged/delete", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ urls }),
      });
      const data = await res.json();
      setOpResult(data);
      if (data.success) fetchStaged();
    } catch (e: any) {
      setOpResult({ success: false, error: e.message });
    } finally {
      setDeletingStaged(false);
      setConfirmDeleteAll(false);
    }
  };

  const handleCleanup = async (tableName: string) => {
    setCleaningUp(tableName);
    setOpResult(null);
    try {
      const res = await fetch(`/api/crawler/cleanup/${encodeURIComponent(tableName)}`, { method: "POST" });
      const data = await res.json();
      setOpResult(data);
      if (data.success) {
        // Refresh source file list
        const srcRes = await fetch(`/api/tables/${encodeURIComponent(tableName)}/sources`);
        const srcData = await srcRes.json();
        setSourceFiles(srcData.sources || []);
        fetchTables();
      }
    } catch (e: any) {
      setOpResult({ success: false, error: e.message });
    } finally {
      setCleaningUp(null);
    }
  };

  const handleRedownload = async (tableName: string) => {
    setRedownloading(tableName);
    setOpResult(null);
    try {
      const res = await fetch(`/api/crawler/redownload/${encodeURIComponent(tableName)}`, { method: "POST" });
      const data = await res.json();
      setOpResult(data);
      if (data.success) {
        const srcRes = await fetch(`/api/tables/${encodeURIComponent(tableName)}/sources`);
        const srcData = await srcRes.json();
        setSourceFiles(srcData.sources || []);
        fetchStaged();
      }
    } catch (e: any) {
      setOpResult({ success: false, error: e.message });
    } finally {
      setRedownloading(null);
    }
  };

  const doAction = async (table: string, action: string) => {
    setOpResult(null);
    try {
      const res = await fetch(
        `/api/tables/${encodeURIComponent(table)}/${action}`,
        { method: "POST" },
      );
      const data = await res.json();
      setOpResult(data);
      if (data.success) fetchTables();
    } catch (e: any) {
      setOpResult({ success: false, error: e.message });
    }
  };

  const previewExpire = async () => {
    setExpirePreview(null);
    try {
      const res = await fetch(
        `/api/ducklake/expire-snapshots?days=${days}&dry_run=true`,
        { method: "POST" },
      );
      const data = await res.json();
      if (data.success) setExpirePreview(data);
      else setOpResult(data);
    } catch (e: any) {
      setOpResult({ success: false, error: e.message });
    }
  };

  const runExpire = async () => {
    try {
      const res = await fetch(
        `/api/ducklake/expire-snapshots?days=${days}`,
        { method: "POST" },
      );
      const data = await res.json();
      setOpResult(data);
      if (data.success) fetchTables();
    } catch (e: any) {
      setOpResult({ success: false, error: e.message });
    } finally {
      setShowExpire(false);
      setExpirePreview(null);
    }
  };

  const showPreview = async (table: string) => {
    setPreview(null);
    try {
      const res = await fetch(
        `/api/tables/${encodeURIComponent(table)}?limit=20`,
      );
      setPreview(await res.json());
    } catch (e) {
      console.error(e);
    }
  };

  const regularTables = tables.filter((t) => !t.is_reject);
  const rejectTables = tables.filter((t) => t.is_reject);

  if (loading) {
    return (
      <div className="flex items-center gap-3 p-6 text-neutral-500">
        <RefreshCw className="w-4 h-4 animate-spin" />
        <span className="text-sm">Loading tables...</span>
      </div>
    );
  }

  return (
    <div className="space-y-4">
      {/* Catalog toolbar */}
      <div className="flex items-center gap-2">
        <h2 className="text-sm font-medium text-neutral-400">DuckLake Catalog</h2>
        <div className="ml-auto flex items-center gap-1">
          <button
            onClick={fetchTables}
            className="flex items-center gap-1.5 px-2.5 py-1 text-xs text-neutral-500 hover:text-neutral-300 hover:bg-neutral-800 rounded transition-colors"
          >
            <RefreshCw className="w-3 h-3" /> Refresh
          </button>
          <button
            onClick={() => { setShowExpire(true); setExpirePreview(null); }}
            className="flex items-center gap-1.5 px-2.5 py-1 text-xs text-amber-500/80 hover:text-amber-400 hover:bg-neutral-800 rounded transition-colors"
          >
            <Trash2 className="w-3 h-3" /> Expire snapshots…
          </button>
        </div>
      </div>

      {/* Table list */}
      <div className="border border-neutral-800 rounded-lg overflow-hidden">
        <div className="grid grid-cols-[1fr_auto_auto_auto_auto_auto] gap-3 px-3 py-2 text-[11px] uppercase tracking-wider text-neutral-600 bg-neutral-900/50 border-b border-neutral-800">
          <span>Table</span>
          <span className="w-28 text-right">Snapshot</span>
          <span className="w-20 text-right">Rows</span>
          <span className="w-16 text-right">Files</span>
          <span className="w-20 text-right">Size</span>
          <span className="w-24" />
        </div>

        {regularTables.length === 0 && (
          <div className="px-3 py-6 text-center text-xs text-neutral-600">
            No tables yet. Run a pipeline first.
          </div>
        )}

        {regularTables.map((t) => (
          <div key={t.name} className="divide-y divide-neutral-800/30">
            <div className={`grid grid-cols-[1fr_auto_auto_auto_auto_auto] gap-3 px-3 py-2 items-center text-sm hover:bg-neutral-900/50 transition-colors ${
              selectedTable === t.name ? "bg-neutral-900/50" : ""
            }`}>
              <button
                onClick={() => fetchSnapshots(t.name)}
                className="flex items-center gap-2 text-neutral-300 hover:text-neutral-100 text-left"
              >
                <Database className="w-3.5 h-3.5 text-emerald-500" />
                <span className="font-mono text-xs">{t.name}</span>
                <ChevronRight className={`w-3 h-3 text-neutral-600 transition-transform ${
                  selectedTable === t.name ? "rotate-90" : ""
                }`} />
              </button>
              <span
                className="text-xs text-neutral-400 text-right w-28 tabular-nums"
                title={t.last_modified ? `Last change: ${formatDate(t.last_modified)}` : undefined}
              >
                {t.latest_snapshot != null ? `v${t.latest_snapshot}` : "—"}
              </span>
              <span className="text-xs text-neutral-400 text-right w-20 tabular-nums">
                {t.rows.toLocaleString()}
              </span>
              <span className="text-xs text-neutral-500 text-right w-16">
                {t.file_count}
              </span>
              <span className="text-xs text-neutral-500 text-right w-20 tabular-nums">
                {formatSize(t.total_size)}
              </span>
              <div className="flex gap-1 w-24 justify-end">
                <button
                  onClick={() => showPreview(t.name)}
                  className="px-2 py-0.5 text-[11px] text-neutral-500 hover:text-neutral-300 hover:bg-neutral-800 rounded transition-colors"
                  title="Preview rows"
                >
                  <Eye className="w-3 h-3" />
                </button>
                <button
                  onClick={() => doAction(t.name, "compact")}
                  className="px-2 py-0.5 text-[11px] text-neutral-500 hover:text-neutral-300 hover:bg-neutral-800 rounded transition-colors"
                  title="Compact (merge adjacent files)"
                >
                  <HardDrive className="w-3 h-3" />
                </button>
                <button
                  onClick={() => doAction(t.name, "reindex")}
                  className="px-2 py-0.5 text-[11px] text-neutral-500 hover:text-neutral-300 hover:bg-neutral-800 rounded transition-colors"
                  title="Rewrite data files (rebuild stats)"
                >
                  <RefreshCw className="w-3 h-3" />
                </button>
              </div>
            </div>

            {/* Snapshot timeline (expanded) */}
            {selectedTable === t.name && (
              <div className="px-3 py-2 bg-neutral-900/30">
                {snapLoading ? (
                  <span className="text-xs text-neutral-600">Loading snapshots...</span>
                ) : snapshots.length > 0 ? (
                  <div className="space-y-1">
                    <div className="flex items-center gap-2 text-[11px] text-neutral-600 mb-1">
                      <Clock className="w-3 h-3" />
                      <span>{snapshots.length} change{snapshots.length !== 1 ? "s" : ""}</span>
                    </div>
                    {snapshots.slice(0, 8).map((s) => (
                      <div key={`${s.snapshot_id}-${s.change}`} className="flex items-center gap-2 text-xs">
                        <span className="text-neutral-600 font-mono w-8">v{s.snapshot_id}</span>
                        <span className="text-neutral-500">{formatDate(s.timestamp)}</span>
                        <span className="text-neutral-600 font-mono text-[11px]">{s.change}</span>
                      </div>
                    ))}
                    {snapshots.length > 8 && (
                      <span className="text-xs text-neutral-700">
                        +{snapshots.length - 8} more
                      </span>
                    )}
                  </div>
                ) : (
                  <span className="text-xs text-neutral-600">No snapshot history</span>
                )}

                {/* Source files section */}
                {sourceError && (
                  <div className="mt-3 pt-3 border-t border-neutral-800/50 text-[11px] text-red-400">
                    Source files unavailable: {sourceError}
                  </div>
                )}
                {!sourceLoading && !sourceError && sourceFiles.length > 0 && (
                  <div className="mt-3 pt-3 border-t border-neutral-800/50">
                    <div className="flex items-center gap-2 text-[11px] text-neutral-600 mb-2">
                      <FileArchive className="w-3 h-3" />
                      <span>Originals in staging — {sourceFiles.length} file{sourceFiles.length !== 1 ? "s" : ""}</span>
                      {sourceFiles.some((f) => f.status === "cleaned") && (
                        <span className="text-amber-500">
                          ({sourceFiles.filter((f) => f.status === "cleaned").length} cleared)
                        </span>
                      )}
                    </div>
                    {sourceFiles.slice(0, 5).map((sf) => (
                      <div key={sf.url} className="flex items-center gap-2 text-[11px]">
                        <span
                          className={`w-2 h-2 rounded-full shrink-0 ${
                          sf.status === "done" ? "bg-emerald-500"
                          : sf.status === "cleaned" ? "bg-amber-500"
                          : "bg-neutral-600"
                        }`}
                          title={sf.status === "done"
                            ? "Loaded — original in staging"
                            : sf.status === "cleaned"
                              ? "Cleared — deleted from staging"
                              : sf.status}
                        />
                        <span className="text-neutral-400 truncate flex-1" title={sf.url}>
                          {sf.file_name}
                        </span>
                        <span className="text-neutral-600">{formatSize(sf.file_size)}</span>
                      </div>
                    ))}
                    {sourceFiles.length > 5 && (
                      <span className="text-[11px] text-neutral-700">
                        +{sourceFiles.length - 5} more
                      </span>
                    )}
                    <div className="flex gap-1.5 mt-2">
                      {sourceFiles.some((f) => f.status === "done") && (
                        <button
                          onClick={() => handleCleanup(t.name)}
                          disabled={cleaningUp === t.name}
                          className="flex items-center gap-1 px-2 py-0.5 text-[11px] text-amber-400 hover:text-amber-300 hover:bg-neutral-800 rounded transition-colors disabled:opacity-40"
                        >
                          <Trash2 className="w-3 h-3" />
                          {cleaningUp === t.name ? "Clearing..." : "Clear staging"}
                        </button>
                      )}
                      {sourceFiles.some((f) => f.status === "cleaned") && (
                        <button
                          onClick={() => handleRedownload(t.name)}
                          disabled={redownloading === t.name}
                          className="flex items-center gap-1 px-2 py-0.5 text-[11px] text-blue-400 hover:text-blue-300 hover:bg-neutral-800 rounded transition-colors disabled:opacity-40"
                        >
                          <Download className="w-3 h-3" />
                          {redownloading === t.name ? "Downloading..." : "Re-download"}
                        </button>
                      )}
                    </div>
                  </div>
                )}
              </div>
            )}
          </div>
        ))}
      </div>

      {/* Reject tables */}
      {rejectTables.length > 0 && (
        <div className="border border-neutral-800 rounded-lg overflow-hidden">
          <div className="px-3 py-2 text-[11px] uppercase tracking-wider text-neutral-600 bg-neutral-900/50 border-b border-neutral-800 flex items-center gap-2">
            <AlertTriangle className="w-3 h-3 text-amber-500" />
            <span>Reject Tables</span>
          </div>
          {rejectTables.map((t) => (
            <div key={t.name} className="grid grid-cols-[1fr_auto_auto] gap-3 px-3 py-2 items-center text-sm hover:bg-neutral-900/50">
              <span className="font-mono text-xs text-neutral-400">{t.name}</span>
              <span className="text-xs text-neutral-500">{t.rows.toLocaleString()} rows</span>
              <button
                onClick={() => showPreview(t.name)}
                className="px-2 py-0.5 text-[11px] text-amber-400 hover:text-amber-300 hover:bg-neutral-800 rounded transition-colors"
              >
                Inspect
              </button>
            </div>
          ))}
        </div>
      )}

      {/* Download staging */}
      {staged && staged.total_files > 0 && (
        <div className="border border-neutral-800 rounded-lg overflow-hidden">
          <div className="px-3 py-2 text-[11px] uppercase tracking-wider text-neutral-600 bg-neutral-900/50 border-b border-neutral-800 flex items-center gap-2">
            <FileArchive className="w-3 h-3 text-neutral-500" />
            <span>
              Download staging — {staged.total_files} file{staged.total_files !== 1 ? "s" : ""}," "
              {formatSize(staged.total_size)}
            </span>
          </div>
          {staged.not_loaded.length > 0 && (
            <div className="px-3 py-2 space-y-1">
              <div className="text-[11px] text-neutral-600 mb-1">
                Not yet loaded — {staged.not_loaded.length} file{staged.not_loaded.length !== 1 ? "s" : ""}
              </div>
              {staged.not_loaded.map((f) => (
                <div key={f.url} className="flex items-center gap-2 text-[11px]">
                  <span className="w-2 h-2 rounded-full shrink-0 bg-neutral-500"
                    title="Staged — not loaded into any table" />
                  <span className="text-neutral-400 truncate flex-1" title={f.local_path}>
                    {f.file_name}
                  </span>
                  <span className="text-neutral-600">{formatSize(f.file_size)}</span>
                  {onLoadToPipeline && (
                    <button
                      onClick={() => onLoadToPipeline(f.local_path)}
                      className="px-2 py-0.5 text-[11px] text-emerald-400 hover:text-emerald-300 hover:bg-neutral-800 rounded transition-colors"
                      title="Scan this file's directory into the pipeline"
                    >
                      Load into table…
                    </button>
                  )}
                  <button
                    onClick={() => handleDeleteStaged([f.url])}
                    disabled={deletingStaged}
                    className="px-2 py-0.5 text-[11px] text-red-400/80 hover:text-red-300 hover:bg-neutral-800 rounded transition-colors disabled:opacity-40"
                    title="Delete from staging (keeps URL for re-crawl)"
                  >
                    Delete
                  </button>
                </div>
              ))}
              <div className="pt-1.5 flex items-center gap-2">
                <button
                  onClick={() => {
                    if (confirmDeleteAll) {
                      handleDeleteStaged(staged.not_loaded.map((f) => f.url));
                    } else {
                      setConfirmDeleteAll(true);
                    }
                  }}
                  disabled={deletingStaged}
                  className={`px-2 py-0.5 text-[11px] rounded transition-colors disabled:opacity-40 ${
                    confirmDeleteAll
                      ? "text-red-300 bg-red-500/10 border border-red-500/30"
                      : "text-red-400/80 hover:text-red-300 hover:bg-neutral-800"
                  }`}
                >
                  {deletingStaged
                    ? "Deleting..."
                    : confirmDeleteAll
                      ? `Confirm delete ${staged.not_loaded.length} file${staged.not_loaded.length !== 1 ? "s" : ""}?`
                      : "Delete all staged"}
                </button>
                {confirmDeleteAll && !deletingStaged && (
                  <button
                    onClick={() => setConfirmDeleteAll(false)}
                    className="px-2 py-0.5 text-[11px] text-neutral-500 hover:text-neutral-300 rounded transition-colors"
                  >
                    Cancel
                  </button>
                )}
              </div>
            </div>
          )}
        </div>
      )}

      {/* Operation result */}
      {opResult && (
        <div className={`flex items-start gap-2 p-3 rounded-lg text-xs ${
          opResult.success
            ? "bg-emerald-500/10 border border-emerald-500/20 text-emerald-300"
            : opResult.unsupported
              ? "bg-amber-500/10 border border-amber-500/20 text-amber-300"
              : "bg-red-500/10 border border-red-500/20 text-red-400"
        }`}>
          {opResult.unsupported && <Info className="w-3.5 h-3.5 shrink-0 mt-0.5" />}
          <span>{opResult.success ? opResult.message : opResult.error}</span>
        </div>
      )}

      {/* Preview modal */}
      {preview && (
        <div className="border border-neutral-700 rounded-lg overflow-hidden">
          <div className="flex items-center justify-between px-3 py-2 bg-neutral-900 border-b border-neutral-800">
            <span className="text-xs text-neutral-400">
              Preview — {preview.total_rows?.toLocaleString()} rows
            </span>
            <button
              onClick={() => setPreview(null)}
              className="text-xs text-neutral-600 hover:text-neutral-400"
            >
              ✕
            </button>
          </div>
          <div className="overflow-x-auto max-h-64">
            <table className="w-full text-xs">
              <thead>
                <tr className="border-b border-neutral-800">
                  {preview.columns.map((c) => (
                    <th key={c} className="px-3 py-1.5 text-left font-medium text-neutral-500 font-mono whitespace-nowrap">
                      {c}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody className="divide-y divide-neutral-800/30">
                {preview.rows.map((row, i) => (
                  <tr key={i} className="hover:bg-neutral-900/50">
                    {preview.columns.map((c) => (
                      <td key={c} className="px-3 py-1 text-neutral-400 font-mono whitespace-nowrap max-w-[200px] truncate">
                        {String(row[c] ?? "")}
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {/* Expire snapshots dialog (catalog-wide) */}
      {showExpire && (
        <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50">
          <div className="bg-neutral-900 border border-neutral-700 rounded-xl p-6 w-96 space-y-4">
            <div className="flex items-start gap-3">
              <AlertTriangle className="w-5 h-5 text-amber-400 shrink-0 mt-0.5" />
              <div>
                <p className="text-sm font-medium text-neutral-200">Expire Snapshots</p>
                <p className="text-xs text-neutral-500 mt-1">
                  Permanently expires snapshots older than the threshold for the
                  entire catalog and reclaims disk space. This cannot be undone.
                </p>
              </div>
            </div>
            <div>
              <label className="text-xs text-neutral-500">Older than (days)</label>
              <input
                type="number"
                value={days}
                onChange={(e) => { setDays(Number(e.target.value)); setExpirePreview(null); }}
                min={1}
                className="w-full mt-1 px-3 py-1.5 bg-neutral-800 border border-neutral-700 rounded text-sm text-neutral-200 font-mono"
              />
            </div>
            {expirePreview && (
              <p className="text-xs text-amber-300/80">
                {expirePreview.snapshots_expired === 0
                  ? "No snapshots would be expired."
                  : `${expirePreview.snapshots_expired} snapshot(s) would be expired: ` +
                    expirePreview.snapshot_ids.map((id) => `v${id}`).join(", ")}
              </p>
            )}
            <div className="flex gap-2 justify-end">
              <button
                onClick={() => { setShowExpire(false); setExpirePreview(null); }}
                className="px-3 py-1.5 text-xs text-neutral-400 hover:text-neutral-200 rounded-lg transition-colors"
              >
                Cancel
              </button>
              <button
                onClick={previewExpire}
                className="px-3 py-1.5 text-xs text-neutral-300 hover:bg-neutral-800 border border-neutral-700 rounded-lg transition-colors"
              >
                Preview
              </button>
              <button
                onClick={runExpire}
                disabled={!expirePreview || expirePreview.snapshots_expired === 0}
                className="px-3 py-1.5 text-xs bg-amber-600 hover:bg-amber-500 disabled:opacity-40 disabled:hover:bg-amber-600 text-white rounded-lg font-medium transition-colors"
              >
                Expire
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
