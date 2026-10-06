import { Badge, type BadgeIntent } from "@/components/ui/Badge";

// Realtime connection state. `polling` is the honest label when the socket is down but the
// HTTP poll is the working path (design §6.9) — informational, not alarming.
export type ConnectionStatus = "live" | "polling" | "reconnecting" | "error";

const MAP: Record<ConnectionStatus, { intent: BadgeIntent; label: string; pulse: boolean }> = {
  live:         { intent: "positive", label: "live",         pulse: true  },
  polling:      { intent: "info",     label: "polling",      pulse: false },
  reconnecting: { intent: "warning",  label: "reconnecting", pulse: true  },
  error:        { intent: "negative", label: "error",        pulse: false },
};

export function ConnectionChip({
  status, label, title, className,
}: { status: ConnectionStatus; label?: string; title?: string; className?: string }) {
  const m = MAP[status];
  return (
    <span role="status" aria-live="polite" className={className}>
      <Badge intent={m.intent} status={status} dot={m.pulse ? "pulse" : true} title={title}>
        {label ?? m.label}
      </Badge>
    </span>
  );
}
