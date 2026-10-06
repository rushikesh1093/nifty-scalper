import { cn } from "@/lib/utils";

// Only reachable after a *successful* response (DataSurface state 5). Title is required.
export function EmptyState({
  title, hint, icon, className,
}: { title: string; hint?: string; icon?: React.ReactNode; className?: string }) {
  return (
    <div className={cn("flex flex-col items-center justify-center gap-1.5 py-8 text-center", className)}>
      {icon && <div aria-hidden="true" className="mb-1 text-text-3">{icon}</div>}
      <div className="text-body font-medium text-text-2">{title}</div>
      {hint && <div className="max-w-md text-table text-text-3">{hint}</div>}
    </div>
  );
}
