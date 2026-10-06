import { ChartSkeleton, TableSkeleton } from "@/components/ui/Skeleton";

// Route-level skeleton shown while a route segment loads.
export default function Loading() {
  return (
    <div className="mx-auto max-w-[1900px] space-y-4 p-4">
      <div className="panel p-4"><ChartSkeleton height={260} /></div>
      <div className="panel p-4"><TableSkeleton rows={6} cols={8} /></div>
    </div>
  );
}
