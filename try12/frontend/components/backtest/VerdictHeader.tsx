import { Badge } from "@/components/ui/Badge";
import { EmptyState } from "@/components/ui/EmptyState";
import { Panel } from "@/components/ui/Panel";
import { ProvenanceBanner } from "./ProvenanceBanner";
import { SourceMarker } from "./ChartWithTable";
import { RECOMMENDATION_INTENT } from "./format";
import type { RunDetail } from "@/types/backtest";

// The verdict (design §5.4, §6.2): recommendation chip, the report's recommendation line, one chip
// per failing criterion and one per cap. The provenance banner always sits above it (C6).

export function VerdictHeader({ run, lead }: {
  run: RunDetail;
  /** Run-level notices (zero-tradeable-day, status) rendered between the banner and the verdict. */
  lead?: React.ReactNode;
}) {
  const v = run.validation;
  const criteria = v?.sections?.["11"]?.criteria ?? [];
  const nameOf = (n: number) => criteria.find((c) => c.n === n)?.name;
  const rec = v?.recommendation ?? v?.sections?.["11"]?.recommendation;
  const line = v?.recommendation_line ?? v?.sections?.["11"]?.line;
  const failing = v?.failing_criteria ?? criteria.filter((c) => !c.passed).map((c) => c.n);
  const caps = v?.caps ?? v?.sections?.["11"]?.caps ?? [];

  return (
    <div className="space-y-3">
      <ProvenanceBanner state={run.synthetic_state} source={run.synthetic_source} />
      {lead}
      <Panel
        title="Verdict"
        meta={<SourceMarker source={run.validation_source === "artifact" ? "artifact" : null} />}
      >
        {run.validation_source === "none" || !v || !rec ? (
          <EmptyState
            title="No validation report stored for this run"
            hint="The verdict and the six-criteria scorecard come from the run's validation report."
          />
        ) : (
          <div className="space-y-2">
            <div className="flex flex-wrap items-center gap-2">
              <Badge intent={RECOMMENDATION_INTENT[rec] ?? "neutral"} strength="solid" status={rec} className="text-table">
                {rec}
              </Badge>
              {line && <span className="text-body text-text-1">{line}</span>}
            </div>
            {failing.length > 0 && (
              <div className="flex flex-wrap items-center gap-1.5">
                <span className="text-micro font-semibold uppercase text-text-3">Failing</span>
                {failing.map((n) => (
                  <Badge key={n} intent="negative" caps={false} status="FAIL" title={nameOf(n)}>
                    criterion {n}{nameOf(n) ? ` · ${nameOf(n)}` : ""}
                  </Badge>
                ))}
              </div>
            )}
            {caps.length > 0 && (
              <div className="flex flex-wrap items-center gap-1.5">
                <span className="text-micro font-semibold uppercase text-text-3">Caps</span>
                {caps.map((c) => (
                  <Badge key={c} intent="warning" strength="outline" caps={false} title={c}>{c}</Badge>
                ))}
              </div>
            )}
          </div>
        )}
      </Panel>
    </div>
  );
}
