import { useState, useRef, useEffect } from "react";
import {
  Globe, LogIn, Search, Download, StopCircle,
  CheckSquare, Square, FolderOpen, Link,
  AlertTriangle, RefreshCw, ShieldCheck, ShieldOff,
  Play, ChevronRight, Monitor,
} from "lucide-react";
import { wsUrl } from "@/lib/api";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface CrawlFile {
  name: string;
  url: string;
  size: number;
  size_str: string;
  date: string;
  description: string;
}

interface DownloadFileProgress {
  name: string;
  url: string;
  size: number;
  status: "queued" | "downloading" | "done" | "failed" | "stopped";
  progress: number | null;
  downloaded_bytes: number;
  local_path: string;
  error: string;
}

interface DownloadProgress {
  phase: string;
  files: DownloadFileProgress[];
  active_count: number;
  completed_count: number;
  failed_count: number;
  total_count: number;
}

// ---------------------------------------------------------------------------
// Props
// ---------------------------------------------------------------------------

interface CrawlerPanelProps {
  onFilesDownloaded: (downloadDir: string, files: { name: string; path: string }[]) => void;
  onScanDir?: (path: string) => void;
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function formatDate(iso: string): string {
  if (!iso) return "";
  try {
    return new Date(iso).toLocaleDateString("en-US", {
      month: "short", day: "numeric", year: "numeric",
    });
  } catch {
    return iso;
  }
}

function progressPct(p: number | null): string {
  if (p === null || p === undefined) return "...";
  return `${Math.round(Math.max(0, p) * 100)}%`;
}

function progressBarWidth(p: number | null): string {
  if (p === null || p === undefined || p < 0) return "20%";
  return `${Math.max(p * 100, 2)}%`;
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export default function CrawlerPanel({ onFilesDownloaded, onScanDir }: CrawlerPanelProps) {
  // --- Login / connection state ---
  const [authMode, setAuthMode] = useState<"login" | "public">("public");
  const [host, setHost] = useState("");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [loginLoading, setLoginLoading] = useState(false);
  const [loginError, setLoginError] = useState<string | null>(null);
  const [connected, setConnected] = useState(false);
  const [envHasCreds, setEnvHasCreds] = useState(false);

  // --- Discovery state ---
  const [discovering, setDiscovering] = useState(false);
  const [discoveryMode, setDiscoveryMode] = useState<"auto" | "manual">("auto");
  const [discoveredFiles, setDiscoveredFiles] = useState<CrawlFile[]>([]);
  const [currentPage, setCurrentPage] = useState(1);
  const [totalPages, setTotalPages] = useState(1);
  const [hasNext, setHasNext] = useState(false);
  const [discoveryError, setDiscoveryError] = useState<string | null>(null);

  // --- Selection ---
  const [selectedUrls, setSelectedUrls] = useState<Set<string>>(new Set());
  const [fileFilter, setFileFilter] = useState("");

  // --- Download state ---
  const [downloading, setDownloading] = useState(false);
  const [downloadProgress, setDownloadProgress] = useState<DownloadProgress | null>(null);
  const [downloadResult, setDownloadResult] = useState<{
    completed: number; failed: number; stopped: number; download_dir: string;
  } | null>(null);
  const [downloadError, setDownloadError] = useState<string | null>(null);
  const [batchSize, setBatchSize] = useState(3); // smaller default for testing

  // --- Load env credentials on mount ---
  const envLoaded = useRef(false);
  useEffect(() => {
    if (envLoaded.current) return;
    envLoaded.current = true;
    fetch("/api/crawler/env-credentials")
      .then((r) => r.json())
      .then((data) => {
        if (data.url) setHost(data.url);
        if (data.username) setUsername(data.username);
        if (data.has_password) setEnvHasCreds(true);
        // If all env vars present, suggest auth mode
        if (data.url && data.username && data.has_password) {
          setAuthMode("login");
        }
      })
      .catch(() => {});
  }, []);

  // --- Poll session status ---
  useEffect(() => {
    if (!connected) return;
    const interval = setInterval(async () => {
      try {
        const res = await fetch("/api/crawler/session-status");
        const data = await res.json();
        if (!data.authenticated) {
          setConnected(false);
          setDiscoveredFiles([]);
        }
      } catch {}
    }, 30_000);
    return () => clearInterval(interval);
  }, [connected]);

  // --- Connect (login or public) ---
  const handleConnect = async (e: React.FormEvent) => {
    e.preventDefault();
    setLoginLoading(true);
    setLoginError(null);

    try {
      const body: Record<string, unknown> = {
        host: host.trim(),
        username: username.trim(),
        password,
        auth_required: authMode === "login",
      };
      const res = await fetch("/api/crawler/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const data = await res.json();
      if (data.success) {
        setConnected(true);
      } else {
        setLoginError(data.error || "Connection failed");
      }
    } catch (e: any) {
      setLoginError(e.message || "Connection failed");
    } finally {
      setLoginLoading(false);
    }
  };

  const handleDisconnect = async () => {
    await fetch("/api/crawler/logout", { method: "POST" });
    setConnected(false);
    setDiscoveredFiles([]);
    setDownloadProgress(null);
    setDownloadResult(null);
  };

  // --- Discovery ---
  const _discoveryAccum = useRef<CrawlFile[] | null>(null);

  const handleDiscover = async (mode: "auto" | "manual" = discoveryMode) => {
    setDiscovering(true);
    setDiscoveryError(null);
    setDownloadResult(null);

    try {
      const res = await fetch("/api/crawler/discover", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          auto: mode === "auto",
          page_url: host.trim() || undefined,
        }),
      });
      const data = await res.json();

      if (data.error) {
        setDiscoveryError(data.error);
      } else {
        if (mode === "auto") {
          setDiscoveredFiles(data.files || []);
          setCurrentPage(data.current_page);
          setTotalPages(data.total_pages);
          setHasNext(false);
        } else {
          const newFiles: CrawlFile[] = data.files || [];
          if (_discoveryAccum.current) {
            const existing = _discoveryAccum.current;
            const merged = [...existing];
            const seen = new Set(existing.map((f: CrawlFile) => f.url));
            for (const f of newFiles) {
              if (!seen.has(f.url)) { merged.push(f); seen.add(f.url); }
            }
            _discoveryAccum.current = merged;
            setDiscoveredFiles(merged);
          } else {
            _discoveryAccum.current = newFiles;
            setDiscoveredFiles(newFiles);
          }
          setCurrentPage(data.current_page);
          setTotalPages(data.total_pages);
          setHasNext(data.has_next);
        }
      }
    } catch (e: any) {
      setDiscoveryError(e.message || "Discovery failed");
    } finally {
      setDiscovering(false);
    }
  };

  const handleStopDiscovery = async () => {
    await fetch("/api/crawler/discover/stop", { method: "POST" });
    setDiscovering(false);
  };

  // --- Selection ---
  const toggleFile = (url: string) => {
    setSelectedUrls((prev) => {
      const next = new Set(prev);
      if (next.has(url)) next.delete(url);
      else next.add(url);
      return next;
    });
  };

  const selectAll = () => setSelectedUrls(new Set(filtered.map((f) => f.url)));
  const deselectAll = () => setSelectedUrls(new Set());

  const filtered = fileFilter
    ? discoveredFiles.filter((f) => f.name.toLowerCase().includes(fileFilter.toLowerCase()))
    : discoveredFiles;

  // --- Download ---
  const handleDownload = async () => {
    const files = discoveredFiles
      .filter((f) => selectedUrls.has(f.url))
      .map((f) => ({ name: f.name, url: f.url }));

    if (files.length === 0) return;

    setDownloading(true);
    setDownloadError(null);
    setDownloadProgress(null);
    setDownloadResult(null);

    const ws = new WebSocket(wsUrl("/ws/crawler/download"));

    ws.onopen = () => {
      ws.send(JSON.stringify({ files, batch_size: batchSize }));
    };

    ws.onmessage = (event) => {
      const data = JSON.parse(event.data);
      if (data.type === "progress") {
        setDownloadProgress({
          phase: data.phase,
          files: data.files,
          active_count: data.active_count,
          completed_count: data.completed_count,
          failed_count: data.failed_count,
          total_count: data.total_count,
        });
      } else if (data.type === "complete") {
        setDownloadResult({
          completed: data.completed,
          failed: data.failed,
          stopped: data.stopped,
          download_dir: data.download_dir,
        });
        setDownloading(false);
        setDownloadProgress(null);
        const dlFiles = (data.files || [])
          .filter((f: DownloadFileProgress) => f.status === "done" && f.local_path)
          .map((f: DownloadFileProgress) => ({ name: f.name, path: f.local_path }));
        if (dlFiles.length > 0) {
          onFilesDownloaded(data.download_dir, dlFiles);
        }
      } else if (data.type === "error") {
        setDownloadError(data.error);
        setDownloading(false);
      }
    };

    ws.onerror = () => {
      setDownloadError("WebSocket connection failed");
      setDownloading(false);
    };
  };

  const handleStopDownload = async () => {
    await fetch("/api/crawler/download/stop", { method: "POST" });
  };

  // --- Render ---
  return (
    <div className="space-y-6">
      {/* Connection screen */}
      {!connected ? (
        <form onSubmit={handleConnect} className="space-y-4 max-w-md">
          <div className="flex items-center gap-3">
            <Globe className="w-5 h-5 text-neutral-500" />
            <h2 className="text-sm font-medium text-neutral-300">Connect to Source</h2>
          </div>

          {/* Auth mode toggle */}
          <div className="flex rounded-lg bg-neutral-900 border border-neutral-800 p-0.5">
            <button
              type="button"
              onClick={() => setAuthMode("public")}
              className={`flex-1 flex items-center justify-center gap-2 px-3 py-2 rounded-md text-sm font-medium transition-colors ${
                authMode === "public"
                  ? "bg-neutral-800 text-neutral-200"
                  : "text-neutral-500 hover:text-neutral-400"
              }`}
            >
              <Monitor className="w-4 h-4" />
              Public Access
            </button>
            <button
              type="button"
              onClick={() => setAuthMode("login")}
              className={`flex-1 flex items-center justify-center gap-2 px-3 py-2 rounded-md text-sm font-medium transition-colors ${
                authMode === "login"
                  ? "bg-neutral-800 text-neutral-200"
                  : "text-neutral-500 hover:text-neutral-400"
              }`}
            >
              <LogIn className="w-4 h-4" />
              Login Required
            </button>
          </div>

          {/* URL field (always shown) */}
          <div>
            <label className="block text-xs text-neutral-500 mb-1">
              {authMode === "public" ? "Page URL" : "Portal URL"}
            </label>
            <div className="relative">
              <Globe className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-neutral-600" />
              <input
                type="url"
                placeholder={
                  authMode === "public"
                    ? "https://geojson.xyz/"
                    : "https://vworld.kr"
                }
                value={host}
                onChange={(e) => setHost(e.target.value)}
                className="w-full pl-9 pr-3 py-2 bg-neutral-900 border border-neutral-800 rounded-lg text-sm text-neutral-200 placeholder:text-neutral-600 focus:outline-none focus:border-neutral-700 font-mono"
                required
              />
            </div>
          </div>

          {/* Credentials (login mode only) */}
          {authMode === "login" && (
            <>
              <div>
                <label className="block text-xs text-neutral-500 mb-1">Username</label>
                <input
                  type="text"
                  placeholder="Username or ID"
                  value={username}
                  onChange={(e) => setUsername(e.target.value)}
                  className="w-full px-3 py-2 bg-neutral-900 border border-neutral-800 rounded-lg text-sm text-neutral-200 placeholder:text-neutral-600 focus:outline-none focus:border-neutral-700"
                  required
                />
              </div>
              <div>
                <label className="block text-xs text-neutral-500 mb-1">Password</label>
                <input
                  type="password"
                  placeholder={envHasCreds ? "Using VWORLD_PASSWORD env var" : "Password"}
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  className="w-full px-3 py-2 bg-neutral-900 border border-neutral-800 rounded-lg text-sm text-neutral-200 placeholder:text-neutral-600 focus:outline-none focus:border-neutral-700"
                  required={!envHasCreds}
                />
              </div>
              {envHasCreds && (
                <p className="text-[11px] text-blue-400/60">
                  Credentials pre-filled from environment variables.
                </p>
              )}
            </>
          )}

          {loginError && (
            <div className="flex items-start gap-2 p-3 rounded-lg bg-red-500/10 border border-red-500/20 text-xs text-red-400">
              <AlertTriangle className="w-3.5 h-3.5 shrink-0 mt-0.5" />
              {loginError}
            </div>
          )}

          <button
            type="submit"
            disabled={loginLoading || !host.trim() || (authMode === "login" && !username.trim())}
            className="flex items-center gap-2 px-4 py-2 bg-emerald-600 hover:bg-emerald-500 disabled:opacity-40 disabled:cursor-not-allowed rounded-lg text-sm font-medium text-white transition-colors"
          >
            {loginLoading ? <RefreshCw className="w-4 h-4 animate-spin" /> : <Globe className="w-4 h-4" />}
            {loginLoading ? "Connecting..." : authMode === "public" ? "Connect" : "Log In"}
          </button>
        </form>
      ) : (
        <>
          {/* Session bar */}
          <div className="flex items-center gap-3 px-3 py-2 bg-neutral-900/50 border border-neutral-800 rounded-lg">
            <ShieldCheck className="w-4 h-4 text-emerald-400" />
            <span className="text-xs text-emerald-400 font-medium">
              {authMode === "public" ? "Connected" : "Authenticated"}
            </span>
            <span className="text-xs text-neutral-600 font-mono truncate flex-1">
              {host || "public session"}
            </span>
            <button
              onClick={handleDisconnect}
              className="flex items-center gap-1 px-2.5 py-1 text-xs text-neutral-500 hover:text-red-400 hover:bg-neutral-800 rounded transition-colors"
            >
              <ShieldOff className="w-3 h-3" />
              Disconnect
            </button>
          </div>

          {/* Discovery controls */}
          <div className="border border-neutral-800 rounded-lg p-4 space-y-3">
            <div className="flex items-center gap-2">
              <Search className="w-4 h-4 text-neutral-500" />
              <span className="text-xs font-medium uppercase tracking-wider text-neutral-400">
                Discovery
              </span>
            </div>

            <div className="flex items-center gap-2">
              <button
                type="button"
                onClick={() => setDiscoveryMode("auto")}
                className={`px-3 py-1.5 rounded text-xs font-medium transition-colors ${
                  discoveryMode === "auto"
                    ? "bg-neutral-700 text-neutral-200"
                    : "bg-neutral-900 text-neutral-500 hover:text-neutral-400"
                }`}
              >
                Auto (all pages)
              </button>
              <button
                type="button"
                onClick={() => setDiscoveryMode("manual")}
                className={`px-3 py-1.5 rounded text-xs font-medium transition-colors ${
                  discoveryMode === "manual"
                    ? "bg-neutral-700 text-neutral-200"
                    : "bg-neutral-900 text-neutral-500 hover:text-neutral-400"
                }`}
              >
                Manual (one page)
              </button>
            </div>

            <div className="flex items-center gap-2">
              {discovering ? (
                <button
                  onClick={handleStopDiscovery}
                  className="flex items-center gap-2 px-3 py-1.5 bg-red-600/20 hover:bg-red-600/30 border border-red-500/30 rounded text-xs font-medium text-red-400 transition-colors"
                >
                  <StopCircle className="w-3.5 h-3.5" /> Stop
                </button>
              ) : (
                <>
                  <button
                    onClick={() => {
                      _discoveryAccum.current = null;
                      handleDiscover(discoveryMode);
                    }}
                    className="flex items-center gap-2 px-3 py-1.5 bg-neutral-700 hover:bg-neutral-600 rounded text-xs font-medium text-neutral-200 transition-colors"
                  >
                    <Play className="w-3.5 h-3.5" />
                    {discoveryMode === "auto" ? "Discover All" : "Discover Page"}
                  </button>
                  {discoveryMode === "manual" && hasNext && discoveredFiles.length > 0 && (
                    <button
                      onClick={() => handleDiscover("manual")}
                      className="flex items-center gap-2 px-3 py-1.5 bg-neutral-700 hover:bg-neutral-600 rounded text-xs font-medium text-neutral-200 transition-colors"
                    >
                      <ChevronRight className="w-3.5 h-3.5" /> Next Page
                    </button>
                  )}
                </>
              )}
              {totalPages > 1 && (
                <span className="ml-auto text-xs text-neutral-500">
                  Page {currentPage} of {totalPages}
                </span>
              )}
            </div>

            {discovering && (
              <div className="flex items-center gap-2 text-xs text-neutral-500">
                <RefreshCw className="w-3.5 h-3.5 animate-spin" />
                {discoveryMode === "auto" ? `Discovering...` : "Fetching page..."}
              </div>
            )}
            {discoveryError && (
              <div className="flex items-start gap-2 p-2 rounded bg-red-500/10 text-xs text-red-400">
                <AlertTriangle className="w-3 h-3 shrink-0 mt-0.5" /> {discoveryError}
              </div>
            )}
          </div>

          {/* File list */}
          {discoveredFiles.length > 0 && (
            <div className="space-y-2">
              <div className="flex items-center gap-3">
                <div className="relative flex-1">
                  <Search className="absolute left-2.5 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-neutral-600" />
                  <input
                    type="text" placeholder="Filter files..." value={fileFilter}
                    onChange={(e) => setFileFilter(e.target.value)}
                    className="w-full pl-8 pr-3 py-1.5 text-xs bg-neutral-900 border border-neutral-800 rounded-md text-neutral-300 placeholder:text-neutral-600 focus:outline-none focus:border-neutral-700"
                  />
                </div>
                <button
                  onClick={selectedUrls.size === discoveredFiles.length ? deselectAll : selectAll}
                  className="flex items-center gap-1.5 px-2.5 py-1.5 text-xs text-neutral-500 hover:text-neutral-300 rounded-md hover:bg-neutral-900"
                >
                  {selectedUrls.size === discoveredFiles.length ? <Square className="w-3.5 h-3.5" /> : <CheckSquare className="w-3.5 h-3.5" />}
                  {selectedUrls.size === discoveredFiles.length ? "Deselect" : "Select All"}
                </button>
                <span className="text-xs text-neutral-600">{selectedUrls.size} / {discoveredFiles.length}</span>
              </div>

              <div className="border border-neutral-800 rounded-lg overflow-hidden">
                <div className="grid grid-cols-[auto_1fr_auto_auto_auto] gap-2 px-3 py-2 text-[11px] uppercase tracking-wider text-neutral-600 bg-neutral-900/50 border-b border-neutral-800">
                  <span className="w-5" /><span>Name</span>
                  <span className="w-16 text-right">Size</span>
                  <span className="w-24 text-right">Date</span>
                  <span className="w-20 text-right">Status</span>
                </div>
                <div className="divide-y divide-neutral-800/50 max-h-80 overflow-y-auto">
                  {filtered.map((file) => {
                    const isSelected = selectedUrls.has(file.url);
                    const dlFile = downloadProgress?.files?.find((f) => f.url === file.url);
                    return (
                      <div key={file.url}
                        className={`grid grid-cols-[auto_1fr_auto_auto_auto] gap-2 px-3 py-2 items-center text-sm hover:bg-neutral-900/50 cursor-pointer ${isSelected ? "bg-emerald-500/5" : ""}`}
                        onClick={() => toggleFile(file.url)}>
                        <button className="w-5 h-5 flex items-center justify-center"
                          onClick={(e) => { e.stopPropagation(); toggleFile(file.url); }}>
                          {isSelected ? <CheckSquare className="w-4 h-4 text-emerald-500" /> : <Square className="w-4 h-4 text-neutral-700" />}
                        </button>
                        <span className="text-neutral-300 truncate text-xs" title={file.url}>{file.name}</span>
                        <span className="text-xs text-neutral-500 text-right w-16 tabular-nums">
                          {file.size_str || (file.size > 0 ? formatSize(file.size) : "—")}
                        </span>
                        <span className="text-xs text-neutral-600 text-right w-24">{formatDate(file.date) || "—"}</span>
                        <span className="text-xs text-right w-20">
                          {dlFile ? (
                            <span className={
                              dlFile.status === "done" ? "text-emerald-400"
                              : dlFile.status === "failed" ? "text-red-400"
                              : dlFile.status === "downloading" ? "text-blue-400"
                              : "text-neutral-500"
                            }>
                              {dlFile.status === "downloading" ? progressPct(dlFile.progress) : dlFile.status}
                            </span>
                          ) : <span className="text-neutral-700">—</span>}
                        </span>
                      </div>
                    );
                  })}
                </div>
              </div>

              {/* Download controls */}
              <div className="border border-neutral-800 rounded-lg p-4 space-y-3">
                <div className="flex items-center gap-2">
                  <Download className="w-4 h-4 text-neutral-500" />
                  <span className="text-xs font-medium uppercase tracking-wider text-neutral-400">Download Queue</span>
                </div>

                <div className="flex items-center gap-3">
                  <div className="flex items-center gap-2">
                    <label className="text-xs text-neutral-500">Batch:</label>
                    <select value={batchSize} onChange={(e) => setBatchSize(Number(e.target.value))}
                      className="px-2 py-1 bg-neutral-900 border border-neutral-800 rounded text-xs text-neutral-300">
                      {[1, 2, 3, 5, 8].map((n) => <option key={n} value={n}>{n}</option>)}
                    </select>
                  </div>
                  {downloading ? (
                    <button onClick={handleStopDownload}
                      className="flex items-center gap-2 px-3 py-1.5 bg-red-600/20 hover:bg-red-600/30 border border-red-500/30 rounded text-xs font-medium text-red-400 transition-colors">
                      <StopCircle className="w-3.5 h-3.5" /> Stop
                    </button>
                  ) : (
                    <button onClick={handleDownload} disabled={selectedUrls.size === 0}
                      className="flex items-center gap-2 px-3 py-1.5 bg-emerald-600 hover:bg-emerald-500 disabled:opacity-40 disabled:cursor-not-allowed rounded text-xs font-medium text-white transition-colors">
                      <Download className="w-3.5 h-3.5" />
                      Download {selectedUrls.size > 0 ? `(${selectedUrls.size})` : ""}
                    </button>
                  )}
                </div>

                {/* Progress bars */}
                {downloadProgress && (
                  <div className="space-y-2">
                    <div className="flex items-center gap-4 text-xs text-neutral-500">
                      <span>{downloadProgress.completed_count}/{downloadProgress.total_count} done</span>
                      {downloadProgress.active_count > 0 && <span className="text-blue-400">{downloadProgress.active_count} active</span>}
                      {downloadProgress.failed_count > 0 && <span className="text-red-400">{downloadProgress.failed_count} failed</span>}
                    </div>
                    <div className="space-y-1 max-h-40 overflow-y-auto">
                      {downloadProgress.files.map((f) => (
                        <div key={f.url} className="flex items-center gap-2 text-xs">
                          <span className="w-24 truncate text-neutral-500">{f.name}</span>
                          <div className="flex-1 h-1.5 bg-neutral-800 rounded-full overflow-hidden">
                            <div
                              className={`h-full rounded-full transition-all duration-300 ${
                                f.status === "done" ? "bg-emerald-500"
                                : f.status === "failed" ? "bg-red-500"
                                : f.status === "downloading" ? "bg-blue-500 animate-pulse"
                                : "bg-neutral-700"
                              }`}
                              style={{ width: progressBarWidth(f.progress) }}
                            />
                          </div>
                          <span className="w-10 text-right text-neutral-600 tabular-nums">
                            {f.status === "done" ? "done"
                              : f.status === "downloading" ? progressPct(f.progress)
                              : f.status}
                          </span>
                        </div>
                      ))}
                    </div>
                  </div>
                )}

                {downloadResult && (
                  <div className="p-3 rounded-lg bg-emerald-500/10 border border-emerald-500/20 text-xs space-y-1">
                    <p className="text-emerald-300 font-medium">Download complete</p>
                    <p className="text-emerald-400/70">
                      {downloadResult.completed} files downloaded
                      {downloadResult.failed > 0 && `, ${downloadResult.failed} failed`}
                    </p>
                    <p className="text-neutral-500 font-mono text-[11px]">{downloadResult.download_dir}</p>
                    <button onClick={() => {
                      if (onScanDir && downloadResult.download_dir) {
                        onScanDir(downloadResult.download_dir);
                      }
                    }}
                      className="inline-flex items-center gap-1.5 mt-1 px-2.5 py-1 bg-neutral-800 hover:bg-neutral-700 rounded text-xs text-neutral-300 transition-colors">
                      <FolderOpen className="w-3 h-3" /> Open in Pipeline
                    </button>
                  </div>
                )}

                {downloadError && (
                  <div className="flex items-start gap-2 p-2 rounded bg-red-500/10 text-xs text-red-400">
                    <AlertTriangle className="w-3 h-3 shrink-0 mt-0.5" /> {downloadError}
                  </div>
                )}
              </div>
            </div>
          )}

          {/* Empty state */}
          {discoveredFiles.length === 0 && !discovering && !discoveryError && (
            <div className="border-2 border-dashed border-neutral-800 rounded-lg p-8 text-center">
              <Link className="w-8 h-8 mx-auto mb-2 text-neutral-700" />
              <p className="text-sm text-neutral-500">
                {authMode === "public"
                  ? `Enter a URL and click Discover to find spatial files`
                  : `Click "Discover All" to find downloadable files`}
              </p>
              <p className="text-xs text-neutral-700 mt-1">
                Supports .geojson, .shp, .gpkg, .parquet, and .zip files
              </p>
            </div>
          )}
        </>
      )}
    </div>
  );
}
