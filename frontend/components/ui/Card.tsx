import { cn } from "@/lib/utils";
interface CardProps { children: React.ReactNode; title?: string; className?: string; }
export function Card({ children, title, className }: CardProps) {
  return (
    <div className={cn("rounded-xl overflow-hidden", className)}
      style={{ background: "var(--surface)", border: "1px solid var(--border)" }}>
      {title && (
        <div className="px-4 py-3" style={{ borderBottom: "1px solid var(--border)" }}>
          <h3 className="text-[11px] font-semibold uppercase tracking-wider" style={{ color: "var(--text-2)" }}>{title}</h3>
        </div>
      )}
      <div className="p-4">{children}</div>
    </div>
  );
}
