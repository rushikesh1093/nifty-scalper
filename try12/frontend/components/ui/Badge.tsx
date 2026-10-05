import { cn } from "@/lib/utils";
type Variant = "green" | "red" | "yellow" | "blue" | "default";
const V: Record<Variant, { fg: string; bg: string }> = {
  green:   { fg: "var(--green)",  bg: "var(--green-dim)"  },
  red:     { fg: "var(--red)",    bg: "var(--red-dim)"    },
  yellow:  { fg: "var(--yellow)", bg: "var(--yellow-dim)" },
  blue:    { fg: "var(--blue)",   bg: "var(--blue-dim)"   },
  default: { fg: "var(--text-2)", bg: "var(--surface-3)"  },
};
export function Badge({ children, variant = "default" }: { children: React.ReactNode; variant?: Variant }) {
  const { fg, bg } = V[variant];
  return (
    <span className="tag font-semibold" style={{ color: fg, background: bg }}>
      {children}
    </span>
  );
}
