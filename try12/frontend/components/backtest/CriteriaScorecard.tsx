import { Badge } from "@/components/ui/Badge";
import { EmptyState } from "@/components/ui/EmptyState";
import { Panel } from "@/components/ui/Panel";
import { SourceMarker } from "./ChartWithTable";
import type { RunDetail } from "@/types/backtest";

// The six criteria (design §5.4, §6.2) from validation.sections.11.criteria: each carries a text
// verdict (PASS / FAIL), its measured value and the threshold rule — colour is never the only carrier.

export function CriteriaScorecard({ run }: { run: RunDetail }) {
  const criteria = run.validation?.sections?.["11"]?.criteria ?? [];
  return (
    <Panel
      title="Six-criteria scorecard"
      meta={<SourceMarker source={run.validation_source === "artifact" ? "artifact" : null} />}
      flush
    >
      {run.validation_source === "none" || criteria.length === 0 ? (
        <EmptyState title="No validation report stored for this run" />
      ) : (
        <div className="overflow-x-auto">
          <table className="data-table">
            <caption className="sr-only">Validation criteria with verdict, measured value and threshold</caption>
            <thead>
              <tr>
                <th scope="col" className="num">#</th>
                <th scope="col">Criterion</th>
                <th scope="col">Set</th>
                <th scope="col">Verdict</th>
                <th scope="col">Measured</th>
                <th scope="col">Threshold</th>
              </tr>
            </thead>
            <tbody>
              {[...criteria].sort((a, b) => a.n - b.n).map((c) => (
                <tr key={c.n}>
                  <td className="num font-mono">{c.n}</td>
                  <td className="text-text-1">{c.name}</td>
                  <td className="whitespace-nowrap font-mono text-text-2">{c.set}</td>
                  <td>
                    <Badge intent={c.passed ? "positive" : "negative"} status={c.passed ? "PASS" : "FAIL"}>
                      {c.passed ? "PASS" : "FAIL"}
                    </Badge>
                  </td>
                  <td className="font-mono text-text-1">{c.actual === null || c.actual === "" ? "—" : String(c.actual)}</td>
                  <td className="text-text-2">{c.rule}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Panel>
  );
}
