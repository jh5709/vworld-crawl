import { useState } from "react";
import { FolderOpen, Search, CheckSquare, Square } from "lucide-react";

export interface FileEntry {
  name: string;
  size: number;
  date: string;
  path: string;
}

interface FileGridProps {
  files: FileEntry[];
  selected: Set<string>;
  onToggle: (path: string) => void;
  onSelectAll: () => void;
  onDeselectAll: () => void;
  onInspect: (file: FileEntry) => void;
  loading: boolean;
  error: string | null;
}

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function formatDate(iso: string): string {
  try {
    return new Date(iso).toLocaleDateString("en-US", {
      month: "short",
      day: "numeric",
      year: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    });
  } catch {
    return iso;
  }
}

export default function FileGrid({
  files,
  selected,
  onToggle,
  onSelectAll,
  onDeselectAll,
  onInspect,
  loading,
  error,
}: FileGridProps) {
  const [filter, setFilter] = useState("");
  const allSelected = files.length > 0 && selected.size === files.length;

  const filtered = filter
    ? files.filter((f) => f.name.toLowerCase().includes(filter.toLowerCase()))
    : files;

  if (error) {
    return (
      <div className="p-4 rounded-lg bg-red-500/10 border border-red-500/20 text-sm text-red-400">
        {error}
      </div>
    );
  }

  if (loading) {
    return (
      <div className="flex items-center gap-3 p-6 text-neutral-500">
        <div className="w-4 h-4 border-2 border-neutral-700 border-t-neutral-400 rounded-full animate-spin" />
        <span className="text-sm">Scanning directory...</span>
      </div>
    );
  }

  if (files.length === 0) {
    return (
      <div className="p-6 text-center text-sm text-neutral-600">
        <FolderOpen className="w-8 h-8 mx-auto mb-2 opacity-40" />
        <p>No supported files found</p>
        <p className="text-xs mt-1">.zip, .shp, .geojson, .gpkg, .geoparquet files will appear here</p>
      </div>
    );
  }

  return (
    <div className="space-y-2">
      {/* Toolbar */}
      <div className="flex items-center gap-3">
        <div className="relative flex-1">
          <Search className="absolute left-2.5 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-neutral-600" />
          <input
            type="text"
            placeholder="Filter files..."
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
            className="w-full pl-8 pr-3 py-1.5 text-xs bg-neutral-900 border border-neutral-800 rounded-md text-neutral-300 placeholder:text-neutral-600 focus:outline-none focus:border-neutral-700"
          />
        </div>
        <button
          onClick={allSelected ? onDeselectAll : onSelectAll}
          className="flex items-center gap-1.5 px-2.5 py-1.5 text-xs text-neutral-500 hover:text-neutral-300 transition-colors rounded-md hover:bg-neutral-900"
        >
          {allSelected ? (
            <Square className="w-3.5 h-3.5" />
          ) : (
            <CheckSquare className="w-3.5 h-3.5" />
          )}
          {allSelected ? "Deselect All" : "Select All"}
        </button>
        <span className="text-xs text-neutral-600">
          {selected.size} / {files.length}
        </span>
      </div>

      {/* File list */}
      <div className="border border-neutral-800 rounded-lg overflow-hidden">
        <div className="grid grid-cols-[auto_1fr_auto_auto_auto] gap-2 px-3 py-2 text-[11px] uppercase tracking-wider text-neutral-600 bg-neutral-900/50 border-b border-neutral-800">
          <span className="w-5" />
          <span>Name</span>
          <span className="text-right w-16">Size</span>
          <span className="text-right w-28">Date</span>
          <span className="w-16" />
        </div>
        <div className="divide-y divide-neutral-800/50 max-h-64 overflow-y-auto">
          {filtered.map((file) => (
            <div
              key={file.path}
              className={`grid grid-cols-[auto_1fr_auto_auto_auto] gap-2 px-3 py-2 items-center text-sm hover:bg-neutral-900/50 transition-colors cursor-pointer ${
                selected.has(file.path) ? "bg-emerald-500/5" : ""
              }`}
              onClick={() => onToggle(file.path)}
            >
              <button
                className="w-5 h-5 flex items-center justify-center"
                onClick={(e) => {
                  e.stopPropagation();
                  onToggle(file.path);
                }}
              >
                {selected.has(file.path) ? (
                  <CheckSquare className="w-4 h-4 text-emerald-500" />
                ) : (
                  <Square className="w-4 h-4 text-neutral-700" />
                )}
              </button>
              <span className="text-neutral-300 truncate">{file.name}</span>
              <span className="text-xs text-neutral-500 text-right w-16 tabular-nums">
                {formatSize(file.size)}
              </span>
              <span className="text-xs text-neutral-600 text-right w-28 tabular-nums">
                {formatDate(file.date)}
              </span>
              <button
                onClick={(e) => {
                  e.stopPropagation();
                  onInspect(file);
                }}
                className="px-2 py-0.5 text-xs text-neutral-500 hover:text-neutral-300 hover:bg-neutral-800 rounded transition-colors"
              >
                Inspect
              </button>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
