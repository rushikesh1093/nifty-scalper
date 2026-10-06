import { cn } from "@/lib/utils";

// Page title + scope controls. One <h1> per route.
export function PageHeader({
  title, description, actions, className,
}: { title: string; description?: React.ReactNode; actions?: React.ReactNode; className?: string }) {
  return (
    <div className={cn("flex flex-wrap items-end justify-between gap-3", className)}>
      <div>
        <h1 className="text-title font-semibold text-text-1">{title}</h1>
        {description && <p className="mt-0.5 text-table text-text-3">{description}</p>}
      </div>
      {actions && <div className="flex flex-wrap items-center gap-2">{actions}</div>}
    </div>
  );
}
