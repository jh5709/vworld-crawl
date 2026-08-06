import { useState } from "react";
import { Database, Layers } from "lucide-react";
import DirectoryPicker from "@/components/DirectoryPicker";
import CrawlerPanel from "@/components/CrawlerPanel";
import DuckLakeConsole from "@/components/DuckLakeConsole";
import MapPreview from "@/components/MapPreview";
import FileGrid, { type FileEntry } from "@/components/FileGrid";
import SchemaEditor, {
  type ColumnDef,
  type ColumnMapping,
} from "@/components/SchemaEditor";
import { type NodeStatus } from "@/components/PipelineProgress";
import { API, wsUrl } from "@/lib/api";

type View = "picker" | "crawler" | "grid" | "schema" | "console";

export default function App() {
  // Directory scanning
  const [view, setView] = useState<View>("picker");
  const [files, setFiles] = useState<FileEntry[]>([]);
  const [scanPath, setScanPath] = useState("");
  const [scanLoading, setScanLoading] = useState(false);
  const [scanError, setScanError] = useState<string | null>(null);
  const [selected, setSelected] = useState<Set<string>>(new Set());

  // Map preview
  const [mapTable, setMapTable] = useState<string | null>(null);

  // Schema detection
  const [inspectFile, setInspectFile] = useState<FileEntry | null>(null);
  const [columns, setColumns] = useState<ColumnDef[]>([]);
  const [crs, setCrs] = useState("");
  const [geometryType, setGeometryType] = useState("");
  const [rowCount, setRowCount] = useState(0);
  const [validCount, setValidCount] = useState(0);
  const [invalidCount, setInvalidCount] = useState(0);
  const [schemaLoading, setSchemaLoading] = useState(false);
  const [schemaError, setSchemaError] = useState<string | null>(null);

  // Column mapping + preview
  const [mapping, setMapping] = useState<ColumnMapping[]>([]);
  const [previewLoading, setPreviewLoading] = useState(false);
  const [previewColumns, setPreviewColumns] = useState<string[]>([]);
  const [previewRows, setPreviewRows] = useState<Record<string, unknown>[]>([]);
  const [previewError, setPreviewError] = useState<string | null>(null);

  // Pipeline
  const [datasetName, setDatasetName] = useState("");

  // Delta load (upsert) options
  const [deltaMode, setDeltaMode] = useState(false);
  const [dataDate, setDataDate] = useState("");
  const [conflictColumn, setConflictColumn] = useState("");
  const [pipelineLoading, setPipelineLoading] = useState(false);
  const [pipelineResult, setPipelineResult] = useState<{
    success?: boolean;
    rows_loaded?: number;
    rows_rejected?: number;
    error?: string;
  } | null>(null);
  const [pipelineProgress, setPipelineProgress] = useState<{
    phase: string;
    file_index: number;
    total_files: number;
    file_name: string;
    nodes: NodeStatus[];
  } | null>(null);

  // --- Scan directory ---
  const handleScan = async (path: string) => {
    setScanPath(path);
    setScanLoading(true);
    setScanError(null);
    setFiles([]);
    setView("grid");
    setSelected(new Set());
    setInspectFile(null);

    try {
      const res = await fetch(`${API}/api/scan-directory`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path }),
      });
      const data = await res.json();
      if (data.error) {
        setScanError(data.error);
      } else {
        setFiles(data.files ?? []);
      }
    } catch (e: any) {
      setScanError(e.message ?? "Failed to scan directory");
    } finally {
      setScanLoading(false);
    }
  };

  // --- Detect schema ---
  const handleInspect = async (file: FileEntry) => {
    setInspectFile(file);
    setView("schema");
    setSchemaLoading(true);
    setSchemaError(null);
    setColumns([]);
    setMapping([]);
    setPreviewColumns([]);
    setPreviewRows([]);
    setPreviewError(null);

    try {
      const res = await fetch(`${API}/api/detect-schema`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path: file.path }),
      });
      const data = await res.json();
      if (data.error) {
        setSchemaError(data.error);
      } else {
        setColumns(data.columns ?? []);
        setCrs(data.crs ?? "");
        setGeometryType(data.geometry_type ?? "");
        setRowCount(data.row_count ?? 0);
        setValidCount(data.valid_count ?? 0);
        setInvalidCount(data.invalid_count ?? 0);
      }
    } catch (e: any) {
      setSchemaError(e.message ?? "Failed to detect schema");
    } finally {
      setSchemaLoading(false);
    }
  };

  // --- Preview ---
  const handlePreview = async () => {
    if (!inspectFile) return;
    setPreviewLoading(true);
    setPreviewError(null);
    setPreviewColumns([]);
    setPreviewRows([]);

    try {
      const res = await fetch(`${API}/api/preview`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          path: inspectFile.path,
          columns: mapping,
          limit: 10,
        }),
      });
      const data = await res.json();
      if (data.error) {
        setPreviewError(data.error);
      } else {
        setPreviewColumns(data.columns ?? []);
        setPreviewRows(data.rows ?? []);
      }
    } catch (e: any) {
      setPreviewError(e.message ?? "Failed to load preview");
    } finally {
      setPreviewLoading(false);
    }
  };

  // --- Selection ---
  const toggleFile = (path: string) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(path)) next.delete(path);
      else next.add(path);
      return next;
    });
  };

  const selectAll = () => setSelected(new Set(files.map((f) => f.path)));
  const deselectAll = () => setSelected(new Set());

  // --- Run Pipeline (WebSocket) ---
  const handleRunPipeline = () => {
    if (!inspectFile || !datasetName.trim()) return;
    setPipelineLoading(true);
    setPipelineResult(null);
    setPipelineProgress(null);

    const paths = selected.size > 0
      ? Array.from(selected)
      : [inspectFile.path];

    const ws = new WebSocket(wsUrl("/ws/pipeline"));

    ws.onopen = () => {
      ws.send(JSON.stringify({
        paths,
        dataset_name: datasetName.trim(),
        column_mapping: mapping,
        data_date: deltaMode ? dataDate : "",
        write_mode: deltaMode ? "upsert" : "append",
        conflict_columns: deltaMode && conflictColumn ? [conflictColumn] : [],
      }));
    };

    ws.onmessage = (event) => {
      const data = JSON.parse(event.data);
      if (data.type === "progress") {
        setPipelineProgress({
          phase: data.phase,
          file_index: data.file_index,
          total_files: data.total_files,
          file_name: data.file_name,
          nodes: data.nodes,
        });
      } else if (data.type === "complete") {
        setPipelineResult({
          success: data.success,
          rows_loaded: data.rows_loaded,
          rows_rejected: data.rows_rejected,
          error: data.error,
        });
        setPipelineLoading(false);
      } else if (data.type === "error") {
        setPipelineResult({ success: false, error: data.error });
        setPipelineLoading(false);
      }
    };

    ws.onerror = () => {
      setPipelineResult({ success: false, error: "WebSocket connection failed" });
      setPipelineLoading(false);
    };
  };

  // --- Crawler file feed ---
  const handleCrawlerFiles = (_downloadDir: string, dlFiles: { name: string; path: string }[]) => {
    if (dlFiles.length > 0) {
      // Feed downloaded files into the file grid by scanning the dir properly
      const dir = dlFiles[0].path.substring(0, dlFiles[0].path.lastIndexOf("/"));
      if (dir) handleScan(dir);
    }
  };

  // --- Crawler -> scan directory ---
  const handleCrawlerScanDir = (dir: string) => {
    handleScan(dir);
  };

  return (
    <div className="min-h-screen bg-neutral-950 text-neutral-50">
      {/* Header */}
      <header className="border-b border-neutral-900 bg-neutral-950/80 backdrop-blur-sm sticky top-0 z-10">
        <div className="max-w-5xl mx-auto px-6 py-3 flex items-center gap-3">
          <Database className="w-5 h-5 text-emerald-400" />
          <h1 className="text-sm font-semibold tracking-tight">VWorld Crawl</h1>

          {/* Breadcrumb */}
          <div className="flex items-center gap-1.5 ml-4 text-xs text-neutral-600">
            <button
              onClick={() => {
                setView("picker");
                setInspectFile(null);
              }}
              className="hover:text-neutral-400 transition-colors"
            >
              Source
            </button>
            {scanPath && (
              <>
                <span>/</span>
                <button
                  onClick={() => {
                    setView("grid");
                    setInspectFile(null);
                  }}
                  className="hover:text-neutral-400 transition-colors"
                >
                  {scanPath.split("/").pop() || scanPath}
                </button>
              </>
            )}
            {inspectFile && (
              <>
                <span>/</span>
                <span className="text-neutral-400">{inspectFile.name}</span>
              </>
            )}
          </div>

          {/* Nav tabs */}
          <div className="ml-auto flex items-center gap-1">
            {(["picker", "crawler", "grid", "schema", "console"] as const).map((v) => (
              <button
                key={v}
                onClick={() => {
                  if (v === "grid" && files.length > 0) setView("grid");
                  else if (v === "schema" && inspectFile) setView("schema");
                  else if (v === "console") setView("console");
                  else if (v === "crawler") setView("crawler");
                  else setView("picker");
                }}
                className={`px-2.5 py-1 rounded text-xs font-medium transition-colors ${
                  view === v
                    ? "bg-neutral-800 text-neutral-200"
                    : "text-neutral-600 hover:text-neutral-400"
                }`}
              >
                {v === "picker" ? "Local" : v === "crawler" ? "Crawler" : v === "grid" ? "Files" : v === "schema" ? "Schema" : "Console"}
              </button>
            ))}
          </div>
        </div>
      </header>

      {/* Body */}
      <main className="max-w-5xl mx-auto px-6 py-6 space-y-6">
        {view === "picker" && (
          <>
            <DirectoryPicker onScan={handleScan} loading={scanLoading} />
            <div className="mt-4 p-3 rounded-lg border border-neutral-800 bg-neutral-900/30 text-center">
              <button
                onClick={() => setView("crawler")}
                className="text-xs text-neutral-500 hover:text-neutral-300 transition-colors"
              >
                Or use the <span className="text-emerald-400 font-medium">Crawler</span> to download files from VWorld portal →
              </button>
            </div>
          </>
        )}

        {view === "crawler" && (
          <CrawlerPanel onFilesDownloaded={handleCrawlerFiles} onScanDir={handleCrawlerScanDir} />
        )}

        {view === "grid" && (
          <div className="space-y-4">
            <div className="flex items-center gap-2">
              <Layers className="w-4 h-4 text-neutral-600" />
              <h2 className="text-sm font-medium text-neutral-400">
                {scanPath || "Scanned Files"}
              </h2>
              <button
                onClick={() => setView("picker")}
                className="ml-auto text-xs text-neutral-600 hover:text-neutral-400 transition-colors"
              >
                ← Change directory
              </button>
            </div>
            <FileGrid
              files={files}
              selected={selected}
              onToggle={toggleFile}
              onSelectAll={selectAll}
              onDeselectAll={deselectAll}
              onInspect={handleInspect}
              loading={scanLoading}
              error={scanError}
            />
          </div>
        )}

        {view === "schema" && (
          <div className="space-y-4">
            <button
              onClick={() => {
                setView("grid");
                setInspectFile(null);
              }}
              className="text-xs text-neutral-600 hover:text-neutral-400 transition-colors"
            >
              ← Back to files
            </button>
            <SchemaEditor
              file={inspectFile ? { name: inspectFile.name, path: inspectFile.path } : null}
              columns={columns}
              crs={crs}
              geometryType={geometryType}
              rowCount={rowCount}
              validCount={validCount}
              invalidCount={invalidCount}
              error={schemaError}
              loading={schemaLoading}
              mapping={mapping}
              onMappingChange={setMapping}
              onPreview={handlePreview}
              previewLoading={previewLoading}
              previewColumns={previewColumns}
              previewRows={previewRows}
              previewError={previewError}
              datasetName={datasetName}
              onDatasetNameChange={setDatasetName}
              onRunPipeline={handleRunPipeline}
              pipelineLoading={pipelineLoading}
              deltaMode={deltaMode}
              onDeltaModeChange={setDeltaMode}
              dataDate={dataDate}
              onDataDateChange={setDataDate}
              conflictColumn={conflictColumn}
              onConflictColumnChange={setConflictColumn}
              pipelineResult={pipelineResult}
              pipelineProgress={pipelineProgress}
            />
          </div>
        )}

        {view === "console" && (
          <DuckLakeConsole
            onLoadToPipeline={(filePath) => {
              // Scan the staged file's directory into the File Grid
              const dir = filePath.slice(0, filePath.lastIndexOf("/")) || filePath;
              handleScan(dir);
            }}
            onViewMap={(tableName) => setMapTable(tableName)}
          />
        )}

        {/* Map preview overlay */}
        {mapTable && (
          <MapPreview
            tableName={mapTable}
            onClose={() => setMapTable(null)}
          />
        )}
      </main>
    </div>
  );
}
