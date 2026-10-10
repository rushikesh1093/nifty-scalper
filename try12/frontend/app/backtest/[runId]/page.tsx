import { Suspense } from "react";
import { Skeleton } from "@/components/ui/Skeleton";
import { RunDetailView } from "@/components/backtest/RunDetailView";

// Thin Server Component shell (design §5.1): the run id comes from the `params` prop; the client
// content reads ?dataset= through useSearchParams, so it sits inside a <Suspense> boundary.
export default function BacktestRunPage({ params }: { params: { runId: string } }) {
  let runId = params.runId;
  try {
    runId = decodeURIComponent(params.runId);
  } catch {
    // a malformed escape is passed through as-is; the API rejects it with a 400
  }
  return (
    <Suspense fallback={<div className="p-4"><Skeleton label="Loading run" /></div>}>
      <RunDetailView runId={runId} />
    </Suspense>
  );
}
