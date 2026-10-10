import { Suspense } from "react";
import { Skeleton } from "@/components/ui/Skeleton";
import { CompareView } from "@/components/backtest/CompareView";

// Thin Server Component shell (design §5.1): the client content reads ?a= / ?b= through
// useSearchParams, which must sit inside a <Suspense> boundary in Next 14 or prerendering bails out.
export default function BacktestComparePage() {
  return (
    <Suspense fallback={<div className="p-4"><Skeleton label="Loading comparison" /></div>}>
      <CompareView />
    </Suspense>
  );
}
