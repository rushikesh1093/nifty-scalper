"use client";
import { useQuery } from "@tanstack/react-query";
import { healthCheck } from "@/lib/api";
import { ApiError, ROOT_BASE } from "@/lib/http";
import { queryKeys } from "@/lib/queryKeys";
import { Badge } from "@/components/ui/Badge";
import { useStickyQueryError } from "@/hooks/useStickyQueryError";

// Backend reachability via GET {ROOT_BASE}/health (rootHttp — /health is not under /api/v1).
// A 404 means the base URL is misconfigured, not that the backend is down; the chip says so.
export function BackendStatusChip() {
  const query = useQuery({
    queryKey: queryKeys.health,
    queryFn: healthCheck,
    refetchInterval: 10_000,
    retry: 1,
  });
  const { data, isPending } = query;
  const { error, isError } = useStickyQueryError(query);

  let chip: React.ReactNode;
  if (isError) {
    const e = error instanceof ApiError ? error : null;
    if (e?.kind === "http" && e.status === 404) {
      chip = (
        <Badge caps={false} intent="warning" dot status="misconfigured" title={`${ROOT_BASE}/health returned 404 — check NEXT_PUBLIC_API_URL`}>
          API: misconfigured base URL
        </Badge>
      );
    } else if (e?.kind === "http") {
      chip = (
        <Badge caps={false} intent="negative" dot status="http-error" title={e.detail ?? e.message}>
          API error {e.status}
        </Badge>
      );
    } else {
      chip = (
        <Badge caps={false} intent="negative" dot status="unreachable" title={e?.detail ?? undefined}>
          Backend unreachable ({ROOT_BASE})
        </Badge>
      );
    }
  } else if (isPending || !data) {
    chip = <Badge caps={false} intent="neutral" status="pending">API …</Badge>;
  } else {
    const redisDown = data.redis === "down" || data.redis === false;
    const title = `status ${data.status} · ${Object.entries(data)
      .filter(([k]) => k !== "status")
      .map(([k, v]) => `${k}=${String(v)}`)
      .join(" · ")}`;
    const disk = typeof data.disk_free_gb === "number" ? data.disk_free_gb : null;
    chip = redisDown ? (
      <Badge caps={false} intent="warning" dot status="degraded" title={title}>API ok · Redis down</Badge>
    ) : data.low_disk ? (
      <Badge caps={false} intent="negative" dot status="low-disk"
             title={`${title} — a full disk stops the database. Free space on the backend's drive.`}>
        API ok · Disk {disk !== null ? `${disk.toFixed(1)} GB` : "low"}
      </Badge>
    ) : (
      <Badge caps={false} intent="positive" dot status="ok" title={title}>API ok</Badge>
    );
  }

  return (
    <div role="status" aria-live="polite" className="flex items-center px-3" data-testid="backend-chip">
      {chip}
    </div>
  );
}
