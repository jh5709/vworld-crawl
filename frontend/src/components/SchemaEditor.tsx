import { useEffect } from "react";
import { RefreshCw, Eye, EyeOff, AlertTriangle, Play } from "lucide-react";
import PipelineProgress, { type NodeStatus } from "@/components/PipelineProgress";

export interface ColumnDef {
  name: string;
  type: string;
  width: number;
}

export interface ColumnMapping {
  original: string;
  renamed: string;
  drop: boolean;
}

interface SchemaEditorProps {
  file: { name: string; path: string } | null;
  columns: ColumnDef[];
  crs: string;
  geometryType: string;
  rowCount: number;
  validCount: number;
  invalidCount: number;
  error: string | null;
  loading: boolean;
  mapping: ColumnMapping[];
  onMappingChange: (mapping: ColumnMapping[]) => void;
  onPreview: () => void;
  previewLoading: boolean;
  previewColumns: string[];
  previewRows: Record<string, unknown>[];
  previewError: string | null;
  datasetName: string;
  onDatasetNameChange: (name: string) => void;
  onRunPipeline: () => void;
  pipelineLoading: boolean;
  deltaMode: boolean;
  onDeltaModeChange: (v: boolean) => void;
  dataDate: string;
  onDataDateChange: (v: string) => void;
  conflictColumn: string;
  onConflictColumnChange: (v: string) => void;
  pipelineResult: {
    success?: boolean;
    rows_loaded?: number;
    rows_rejected?: number;
    error?: string;
  } | null;
  pipelineProgress: {
    phase: string;
    file_index: number;
    total_files: number;
    file_name: string;
    nodes: NodeStatus[];
  } | null;
}

export default function SchemaEditor({
  file,
  columns,
  crs,
  geometryType,
  rowCount,
  validCount,
  invalidCount,
  error,
  loading,
  mapping,
  onMappingChange,
  onPreview,
  previewLoading,
  previewColumns,
  previewRows,
  previewError,
  datasetName,
  onDatasetNameChange,
  onRunPipeline,
  pipelineLoading,
  deltaMode,
  onDeltaModeChange,
  dataDate,
  onDataDateChange,
  conflictColumn,
  onConflictColumnChange,
  pipelineResult,
  pipelineProgress,
}: SchemaEditorProps) {
  // Init mapping when columns arrive
  useEffect(() => {
    if (columns.length > 0) {
      const existing = new Map(mapping.map((m) => [m.original, m]));
      const merged = columns.map((c) => {
        const prev = existing.get(c.name);
        return prev ?? { original: c.name, renamed: "", drop: false };
      });
      onMappingChange(merged);
    }
  }, [columns]);

  const updateColumn = (index: number, field: "renamed" | "drop", value: string | boolean) => {
    const next = mapping.map((m, i) =>
      i === index ? { ...m, [field]: value } : m
    );
    onMappingChange(next);
  };

  if (!file) {
    return (
      <div className="p-8 text-center text-sm text-neutral-600">
        <p>Select a file and click Inspect to view its schema</p>
      </div>
    );
  }

  if (loading) {
    return (
      <div className="flex items-center gap-3 p-6 text-neutral-500">
        <RefreshCw className="w-4 h-4 animate-spin" />
        <span className="text-sm">Detecting schema...</span>
      </div>
    );
  }

  if (error) {
    return (
      <div className="p-4 rounded-lg bg-red-500/10 border border-red-500/20 flex items-start gap-3">
        <AlertTriangle className="w-4 h-4 text-red-400 shrink-0 mt-0.5" />
        <div>
          <p className="text-sm font-medium text-red-400">Schema Detection Failed</p>
          <p className="text-xs text-red-400/70 mt-0.5">{error}</p>
        </div>
      </div>
    );
  }

  if (columns.length === 0) {
    return (
      <div className="p-6 text-center text-sm text-neutral-600">
        <p>No columns detected</p>
      </div>
    );
  }

  return (
    <div className="space-y-4">
      {/* File info bar */}
      <div className="flex flex-wrap items-center gap-3 px-3 py-2 bg-neutral-900/50 border border-neutral-800 rounded-lg text-xs">
        <span className="text-neutral-300 font-medium">{file.name}</span>
        {crs && (
          <span className="px-1.5 py-0.5 bg-emerald-500/10 text-emerald-400 rounded font-mono">
            {crs}
          </span>
        )}
        {geometryType && (
          <span className="text-neutral-500">{geometryType}</span>
        )}
        <span className="text-neutral-600 ml-auto">
          {rowCount.toLocaleString()} rows · {columns.length} columns
          {(validCount > 0 || invalidCount > 0) && (
            <>
              {" · "}
              <span className="text-emerald-500">{validCount.toLocaleString()} valid</span>
              {invalidCount > 0 && (
                <span className="text-red-400">
                  {", "}{invalidCount.toLocaleString()} invalid
                </span>
              )}
            </>
          )}
        </span>
      </div>

      {/* Invalid geometry warning */}
      {invalidCount > 0 && (
        <div className="flex items-start gap-2 p-3 rounded-lg bg-amber-500/10 border border-amber-500/20 text-xs">
          <AlertTriangle className="w-3.5 h-3.5 text-amber-400 shrink-0 mt-0.5" />
          <div>
            <span className="text-amber-300 font-medium">{invalidCount.toLocaleString()} invalid geometr{invalidCount === 1 ? "y" : "ies"} detected</span>
            <span className="text-amber-400/70 ml-2">— run pipeline to quarantine these into the rejects table</span>
          </div>
        </div>
      )}

      {/* Column editor table */}
      <div className="border border-neutral-800 rounded-lg overflow-hidden">
        <div className="grid grid-cols-[1fr_auto_auto_auto] gap-3 px-3 py-2 text-[11px] uppercase tracking-wider text-neutral-600 bg-neutral-900/50 border-b border-neutral-800">
          <span>Column</span>
          <span className="w-24">Type</span>
          <span className="w-24">Rename To</span>
          <span className="w-12 text-center">Drop</span>
        </div>
        <div className="divide-y divide-neutral-800/50 max-h-80 overflow-y-auto">
          {mapping.map((col, i) => {
            const def = columns[i];
            const isDropped = col.drop;
            return (
              <div
                key={col.original}
                className={`grid grid-cols-[1fr_auto_auto_auto] gap-3 px-3 py-2 items-center text-sm transition-colors ${
                  isDropped ? "opacity-40 bg-neutral-900/30" : ""
                }`}
              >
                {/* Original name */}
                <span className="text-neutral-300 font-mono text-xs truncate">
                  {col.original}
                </span>

                {/* Type */}
                <span className="text-xs text-neutral-500 w-24 font-mono">
                  {def?.type ?? ""}
                </span>

                {/* Rename input */}
                <input
                  type="text"
                  value={col.renamed}
                  onChange={(e) => updateColumn(i, "renamed", e.target.value)}
                  disabled={isDropped}
                  placeholder={col.original}
                  className="w-24 px-2 py-1 text-xs bg-neutral-900 border border-neutral-800 rounded text-neutral-300 font-mono placeholder:text-neutral-700 focus:outline-none focus:border-neutral-600 disabled:opacity-0"
                />

                {/* Drop toggle */}
                <button
                  onClick={() => updateColumn(i, "drop", !col.drop)}
                  className="w-12 flex justify-center p-1 rounded hover:bg-neutral-800 transition-colors"
                  title={isDropped ? "Keep column" : "Drop column"}
                >
                  {isDropped ? (
                    <EyeOff className="w-4 h-4 text-red-400" />
                  ) : (
                    <Eye className="w-4 h-4 text-neutral-600 hover:text-neutral-400" />
                  )}
                </button>
              </div>
            );
          })}
        </div>
      </div>

      {/* Preview button */}
      <button
        onClick={onPreview}
        disabled={previewLoading}
        className="flex items-center gap-2 px-4 py-2 bg-neutral-800 hover:bg-neutral-700 disabled:opacity-50 rounded-lg text-sm font-medium text-neutral-200 transition-colors"
      >
        {previewLoading ? (
          <RefreshCw className="w-4 h-4 animate-spin" />
        ) : (
          <Eye className="w-4 h-4" />
        )}
        Preview ({previewColumns.length > 0 ? previewColumns.length : mapping.filter((c) => !c.drop).length} columns)
      </button>

      {/* Preview error */}
      {previewError && (
        <div className="p-3 rounded-lg bg-red-500/10 border border-red-500/20 text-xs text-red-400">
          {previewError}
        </div>
      )}

      {/* Preview table */}
      {previewRows.length > 0 && (
        <div className="border border-neutral-800 rounded-lg overflow-hidden">
          <div className="px-3 py-2 text-xs text-neutral-600 bg-neutral-900/50 border-b border-neutral-800">
            Preview — first {previewRows.length} rows
            {previewColumns.length > 0 && (
              <span className="ml-2 text-neutral-700">
                ({previewColumns.length} columns)
              </span>
            )}
          </div>
          <div className="overflow-x-auto">
            <table className="w-full text-xs">
              <thead>
                <tr className="border-b border-neutral-800">
                  {(previewColumns.length > 0 ? previewColumns : mapping.filter((c) => !c.drop).map((c) => c.renamed || c.original)).map(
                    (colName) => (
                      <th
                        key={colName}
                        className="px-3 py-2 text-left font-medium text-neutral-400 font-mono whitespace-nowrap"
                      >
                        {colName}
                      </th>
                    )
                  )}
                </tr>
              </thead>
              <tbody className="divide-y divide-neutral-800/30">
                {previewRows.map((row, ri) => (
                  <tr key={ri} className="hover:bg-neutral-900/50">
                    {(previewColumns.length > 0 ? previewColumns : mapping.filter((c) => !c.drop).map((c) => c.renamed || c.original)).map(
                      (colName) => (
                        <td
                          key={colName}
                          className="px-3 py-1.5 text-neutral-300 font-mono whitespace-nowrap max-w-[200px] truncate"
                        >
                          {String(row[colName] ?? "")}
                        </td>
                      )
                    )}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {/* Run Pipeline */}
      <div className="border border-neutral-800 rounded-lg p-4 space-y-3">
        <div className="flex items-center gap-2 text-neutral-400">
          <Play className="w-4 h-4" />
          <span className="text-xs font-medium uppercase tracking-wider">Run Pipeline</span>
        </div>

        <div className="flex items-center gap-3">
          <input
            type="text"
            value={datasetName}
            onChange={(e) => onDatasetNameChange(e.target.value)}
            placeholder="Dataset name (e.g. roads)"
            className="flex-1 px-3 py-2 bg-neutral-900 border border-neutral-800 rounded-lg text-sm text-neutral-200 placeholder:text-neutral-600 focus:outline-none focus:border-neutral-700 font-mono"
          />
          <button
            onClick={onRunPipeline}
            disabled={pipelineLoading || !datasetName.trim()}
            className="flex items-center gap-2 px-4 py-2 bg-emerald-600 hover:bg-emerald-500 disabled:opacity-40 disabled:cursor-not-allowed rounded-lg text-sm font-medium text-white transition-colors"
          >
            {pipelineLoading ? (
              <RefreshCw className="w-4 h-4 animate-spin" />
            ) : (
              <Play className="w-4 h-4" />
            )}
            {pipelineLoading ? "Running..." : "Run Pipeline"}
          </button>
        </div>

        {/* Delta load (upsert) options */}
        <div className="border-t border-neutral-800/50 pt-3">
          <label className="flex items-center gap-2 text-xs text-neutral-400 cursor-pointer">
            <input
              type="checkbox"
              checked={deltaMode}
              onChange={(e) => onDeltaModeChange(e.target.checked)}
              className="rounded border-neutral-700 bg-neutral-900"
            />
            Delta file — upsert merge instead of append
          </label>
          {deltaMode && (
            <div className="flex items-center gap-2 mt-2">
              <input
                type="date"
                value={dataDate}
                onChange={(e) => onDataDateChange(e.target.value)}
                className="px-2.5 py-1.5 bg-neutral-900 border border-neutral-800 rounded-lg text-xs text-neutral-200 focus:outline-none focus:border-neutral-700 font-mono"
                title="Data date (publication date of this delta)"
              />
              <select
                value={conflictColumn}
                onChange={(e) => onConflictColumnChange(e.target.value)}
                className="flex-1 px-2.5 py-1.5 bg-neutral-900 border border-neutral-800 rounded-lg text-xs text-neutral-200 focus:outline-none focus:border-neutral-700 font-mono"
                title="Upsert key column (rows matching this column are replaced)"
              >
                <option value="">Upsert key column…</option>
                {mapping
                  .filter((m) => !m.drop)
                  .map((m) => (
                    <option key={m.original} value={m.renamed || m.original}>
                      {m.renamed || m.original}
                    </option>
                  ))}
              </select>
            </div>
          )}
          {deltaMode && !conflictColumn && (
            <p className="text-[11px] text-amber-500/80 mt-1">
              Select an upsert key column — rows matching it are replaced, others inserted.
            </p>
          )}
        </div>

        {/* Pipeline progress */}
        {pipelineProgress && (
          <PipelineProgress
            phase={pipelineProgress.phase}
            fileIndex={pipelineProgress.file_index}
            totalFiles={pipelineProgress.total_files}
            fileName={pipelineProgress.file_name}
            nodes={pipelineProgress.nodes}
          />
        )}

        {/* Pipeline result */}
        {pipelineResult && (
          <div className={`p-3 rounded-lg text-xs ${
            pipelineResult.success
              ? "bg-emerald-500/10 border border-emerald-500/20"
              : "bg-red-500/10 border border-red-500/20"
          }`}>
            {pipelineResult.success ? (
              <div className="space-y-1">
                <p className="text-emerald-300 font-medium">Pipeline completed</p>
                <p className="text-emerald-400/70">
                  {pipelineResult.rows_loaded?.toLocaleString()} rows loaded
                  {pipelineResult.rows_rejected ? (
                    <span>, {pipelineResult.rows_rejected.toLocaleString()} rejected</span>
                  ) : null}
                </p>
              </div>
            ) : (
              <p className="text-red-400">{pipelineResult.error || "Pipeline failed"}</p>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
