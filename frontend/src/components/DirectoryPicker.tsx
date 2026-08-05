import { useState } from "react";
import { FolderOpen } from "lucide-react";

interface DirectoryPickerProps {
  onScan: (path: string) => void;
  loading: boolean;
}

export default function DirectoryPicker({ onScan, loading }: DirectoryPickerProps) {
  const [path, setPath] = useState("");
  const [dragOver, setDragOver] = useState(false);

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    if (path.trim()) onScan(path.trim());
  };

  const handleDrop = (e: React.DragEvent) => {
    e.preventDefault();
    setDragOver(false);

    // Handle dropped folders via webkitGetAsEntry
    const items = e.dataTransfer.items;
    if (items) {
      for (let i = 0; i < items.length; i++) {
        const item = items[i];
        if (item.kind === "file") {
          const entry = (item as any).webkitGetAsEntry?.();
          if (entry?.isDirectory) {
            // We can't get the full path from browser API, but the
            // file name gives us a hint. Fall through to asking for path.
          }
        }
      }
    }

    // Fallback: try to extract path from dropped files
    const files = e.dataTransfer.files;
    if (files.length > 0) {
      // Some browsers expose the path via a non-standard property
      const firstPath = (files[0] as any).path;
      if (firstPath) {
        const dirPath = firstPath.substring(0, firstPath.lastIndexOf("/"));
        if (dirPath) {
          setPath(dirPath);
          onScan(dirPath);
          return;
        }
      }
    }
  };

  return (
    <div className="space-y-4">
      {/* Path input */}
      <form onSubmit={handleSubmit} className="flex gap-2">
        <div className="relative flex-1">
          <FolderOpen className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-neutral-600" />
          <input
            type="text"
            placeholder="/path/to/shapefiles"
            value={path}
            onChange={(e) => setPath(e.target.value)}
            className="w-full pl-9 pr-3 py-2 bg-neutral-900 border border-neutral-800 rounded-lg text-sm text-neutral-200 placeholder:text-neutral-600 focus:outline-none focus:border-neutral-700 font-mono"
          />
        </div>
        <button
          type="submit"
          disabled={loading || !path.trim()}
          className="px-4 py-2 bg-neutral-800 hover:bg-neutral-700 disabled:opacity-40 disabled:cursor-not-allowed rounded-lg text-sm font-medium text-neutral-200 transition-colors"
        >
          {loading ? "Scanning..." : "Scan"}
        </button>
      </form>

      {/* Drop zone */}
      <div
        onDragOver={(e) => {
          e.preventDefault();
          setDragOver(true);
        }}
        onDragLeave={() => setDragOver(false)}
        onDrop={handleDrop}
        className={`border-2 border-dashed rounded-lg p-6 text-center transition-colors ${
          dragOver
            ? "border-emerald-500/50 bg-emerald-500/5"
            : "border-neutral-800 hover:border-neutral-700"
        }`}
      >
        <FolderOpen className="w-8 h-8 mx-auto mb-2 text-neutral-700" />
        <p className="text-sm text-neutral-500">
          Drag a folder here, or type a path above
        </p>
        <p className="text-xs text-neutral-700 mt-1">
          Supports .zip archives and .shp shapefiles
        </p>
      </div>
    </div>
  );
}
