import { Check, Loader2, X, Circle } from "lucide-react";

export interface NodeStatus {
  name: string;
  status: "pending" | "running" | "done" | "error";
  rows: number;
  error: string | null;
}

interface PipelineProgressProps {
  phase: string;
  fileIndex: number;
  totalFiles: number;
  fileName: string;
  nodes: NodeStatus[];
}

const NODE_LABELS: Record<string, string> = {
  spatial: "Read shapefile",
  dropcol: "Drop columns",
  project: "Select columns",
  rename: "Rename columns",
  sql: "WKB + bbox",
  geomvalidate: "Validate geometry",
  ducklake: "Write to DuckLake",
};

function NodeIcon({ status }: { status: NodeStatus["status"] }) {
  switch (status) {
    case "done":
      return <Check className="w-3.5 h-3.5 text-emerald-400" />;
    case "running":
      return <Loader2 className="w-3.5 h-3.5 text-blue-400 animate-spin" />;
    case "error":
      return <X className="w-3.5 h-3.5 text-red-400" />;
    default:
      return <Circle className="w-3.5 h-3.5 text-neutral-700" />;
  }
}

export default function PipelineProgress({
  phase,
  fileIndex,
  totalFiles,
  fileName,
  nodes,
}: PipelineProgressProps) {
  const phaseLabel = phase === "valid" ? "Valid rows" : "Invalid rows";
  const phaseColor = phase === "valid" ? "emerald" : "amber";

  return (
    <div className="border border-neutral-800 rounded-lg overflow-hidden">
      {/* Header */}
      <div className="flex items-center gap-3 px-3 py-2 bg-neutral-900/50 border-b border-neutral-800">
        <span className={`text-xs font-medium text-${phaseColor}-400`}>
          {phaseLabel}
        </span>
        <span className="text-xs text-neutral-600">
          File {fileIndex + 1} of {totalFiles}
        </span>
        <span className="text-xs text-neutral-500 truncate ml-auto max-w-[200px]">
          {fileName}
        </span>
      </div>

      {/* Node list */}
      <div className="divide-y divide-neutral-800/30">
        {nodes.map((node) => (
          <div
            key={node.name}
            className={`flex items-center gap-3 px-3 py-2 text-xs transition-colors ${
              node.status === "done"
                ? "text-neutral-300"
                : node.status === "running"
                ? "text-blue-300 bg-blue-500/5"
                : node.status === "error"
                ? "text-red-300 bg-red-500/5"
                : "text-neutral-600"
            }`}
          >
            <NodeIcon status={node.status} />
            <span className="flex-1">{NODE_LABELS[node.name] || node.name}</span>
            {node.status === "done" && node.rows > 0 && (
              <span className="text-neutral-500 tabular-nums">
                {node.rows.toLocaleString()} rows
              </span>
            )}
            {node.status === "error" && node.error && (
              <span className="text-red-400 truncate max-w-[150px]">
                {node.error}
              </span>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}
