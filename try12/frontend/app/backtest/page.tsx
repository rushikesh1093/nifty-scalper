import { Suspense } from "react";
import { Skeleton } from "@/components/ui/Skeleton";
import { BacktestHome } from "@/components/backtest/BacktestHome";

// Thin Server Component shell (design §5.1): the client content reads ?dataset= through
// useSearchParams, which must sit inside a <Suspense> boundary in Next 14 or prerendering bails out.
export default function BacktestPage() {
  return (
    <Suspense fallback={<div className="p-4"><Skeleton label="Loading backtests" /></div>}>
      <BacktestHome />
    </Suspense>
  );
}
