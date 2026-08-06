import { useState } from "react";
import { FolderOpen, Globe, Monitor } from "lucide-react";

interface DirectoryPickerProps {
  onScan: (path: string) => void;
  loading: boolean;
}

type SourceMode = "local" | "remote";

export default function DirectoryPicker({ onScan, loading }: DirectoryPickerProps) {
  const [mode, setMode] = useState<SourceMode>("local");
  const [path, setPath] = useState("");
  const [url, setUrl] = useState("");
  const [dragOver, setDragOver] = useState(false);

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    if (mode === "local" && path.trim()) {
      onScan(path.trim());
    } else if (mode === "remote" && url.trim()) {
      onScan(url.trim());
    }
  };

  const handleBrowse = async () => {
    try {
      const res = await fetch("/api/pick-directory");
      const data = await res.json();
      if (data.path) {
        setPath(data.path);
        onScan(data.path);
      }
    } catch {
      // Fall back to manual path entry
    }
  };

  const handleDrop = (e: React.DragEvent) => {
    e.preventDefault();
    setDragOver(false);

    const files = e.dataTransfer.files;
    if (files.length > 0) {
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
      {/* Source mode toggle */}
      <div className="flex rounded-lg bg-neutral-900 border border-neutral-800 p-0.5">
        <button
          onClick={() => setMode("local")}
          className={`flex-1 flex items-center justify-center gap-2 px-3 py-2 rounded-md text-sm font-medium transition-colors ${
            mode === "local"
              ? "bg-neutral-800 text-neutral-200"
              : "text-neutral-500 hover:text-neutral-400"
          }`}
        >
          <Monitor className="w-4 h-4" />
          Local Directory
        </button>
        <button
          onClick={() => setMode("remote")}
          className={`flex-1 flex items-center justify-center gap-2 px-3 py-2 rounded-md text-sm font-medium transition-colors ${
            mode === "remote"
              ? "bg-neutral-800 text-neutral-200"
              : "text-neutral-500 hover:text-neutral-400"
          }`}
        >
          <Globe className="w-4 h-4" />
          Remote URL
        </button>
      </div>

      {mode === "local" ? (
        <>
          {/* Local path input */}
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
              type="button"
              onClick={handleBrowse}
              disabled={loading}
              className="px-3 py-2 bg-neutral-800 hover:bg-neutral-700 disabled:opacity-40 rounded-lg text-sm text-neutral-300 transition-colors"
              title="Open folder picker"
            >
              Browse...
            </button>
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
              Drag a folder, browse, or type a path
            </p>
            <p className="text-xs text-neutral-700 mt-1">
              .zip, .shp, .geojson, .gpkg, .geoparquet files
            </p>
          </div>
        </>
      ) : (
        <>
          {/* Remote URL input */}
          <form onSubmit={handleSubmit} className="flex gap-2">
            <div className="relative flex-1">
              <Globe className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-neutral-600" />
              <input
                type="url"
                placeholder="https://vworld.kr/data/..."
                value={url}
                onChange={(e) => setUrl(e.target.value)}
                className="w-full pl-9 pr-3 py-2 bg-neutral-900 border border-neutral-800 rounded-lg text-sm text-neutral-200 placeholder:text-neutral-600 focus:outline-none focus:border-neutral-700 font-mono"
              />
            </div>
            <button
              type="submit"
              disabled={loading || !url.trim()}
              className="px-4 py-2 bg-neutral-800 hover:bg-neutral-700 disabled:opacity-40 disabled:cursor-not-allowed rounded-lg text-sm font-medium text-neutral-200 transition-colors"
            >
              {loading ? "Connecting..." : "Connect"}
            </button>
          </form>

          <div className="border-2 border-dashed border-neutral-800 rounded-lg p-6 text-center">
            <Globe className="w-8 h-8 mx-auto mb-2 text-neutral-700" />
            <p className="text-sm text-neutral-500">
              Remote crawling is now available in the <span className="text-emerald-400 font-medium">Crawler</span> tab
            </p>
            <p className="text-xs text-neutral-700 mt-1">
              Log in to VWorld, discover, and download files from the portal
            </p>
          </div>
        </>
      )}
    </div>
  );
}
