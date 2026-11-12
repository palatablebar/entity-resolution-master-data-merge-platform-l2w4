import type { RuleComparison, RuleContext } from '../../types/models';
import { formatScore } from '../../utils/formatScore';

const GROUP_LABELS: Record<string, string> = {
  threshold: '阈值',
  field_weights: '字段权重',
  source_reliability: '来源可靠性',
};

const formatRuleValue = (value: number | null): string =>
  value === null || value === undefined ? '—' : formatScore(value);

interface Props {
  comparison: RuleComparison | null | undefined;
  /** 紧凑模式用于列表/详情侧栏 */
  compact?: boolean;
  /** 额外提示：例如该候选是否会受规则变化影响 */
  note?: string | null;
}

/** 运行冻结版本 ↔ 同一规则集当前版本 的并置对照。 */
export const RuleVersionBar = ({ comparison, compact = false, note = null }: Props) => {
  if (!comparison) return null;
  const currentLabel = comparison.current_available
    ? `${comparison.current_rule_name ?? '—'} · v${comparison.current_rule_version ?? '—'}`
    : '当前规则不可用';
  const runLabel = `${comparison.run_rule_name ?? '—'} · v${comparison.run_rule_version ?? '—'}`;
  const changed = comparison.affected_basis;
  const versionOnly = comparison.version_changed && !changed;

  return (
    <div className={`rule-compare${changed ? ' changed' : ''}`}>
      <div className="rule-compare-head">
        <span className="paper-label">规则版本对照</span>
        {comparison.snapshot_missing ? (
          <span className="stamp neutral">旧运行 · 缺冻结快照</span>
        ) : changed ? (
          <span className="stamp press">规则变化影响本卷产物</span>
        ) : versionOnly ? (
          <span className="stamp neutral">版本号变化 · 规则值未变</span>
        ) : (
          <span className="stamp olive">规则未变化</span>
        )}
      </div>
      <div className="rule-compare-grid">
        <div className="rule-compare-cell">
          <div className="dim" style={{ fontSize: 11 }}>本卷冻结版本</div>
          <div className="mono soft">{runLabel}</div>
        </div>
        <div className="rule-compare-arrow">{changed ? '→' : '='}</div>
        <div className="rule-compare-cell">
          <div className="dim" style={{ fontSize: 11 }}>规则集当前版本</div>
          <div className="mono soft">{currentLabel}</div>
        </div>
      </div>

      {changed && comparison.changes.length > 0 && !compact ? (
        <table className="table" style={{ marginTop: 6 }}>
          <thead>
            <tr>
              <th>项</th>
              <th>本卷冻结</th>
              <th>当前</th>
            </tr>
          </thead>
          <tbody>
            {comparison.changes.map((change) => (
              <tr key={change.key}>
                <td className="dim">
                  {GROUP_LABELS[change.group] ?? change.group} · {change.label}
                </td>
                <td className="mono">{formatRuleValue(change.before)}</td>
                <td className="mono" style={{ color: 'var(--vermilion)' }}>
                  {formatRuleValue(change.after)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : null}

      {note ? (
        <div className="rule-compare-note" style={{ marginTop: 8 }}>
          {note}
        </div>
      ) : null}
    </div>
  );
};

export const RuleVersionBadge = ({
  context,
  impact,
}: {
  context: RuleContext | null | undefined;
  impact?: { affected: boolean; reason: string } | null;
}) => {
  if (!context?.comparison?.rule_changed) return null;
  const affected = Boolean(impact?.affected);
  return (
    <span
      className={`stamp ${affected ? '' : 'neutral'}`}
      title={impact?.reason ?? context.comparison.changes.map((change) => change.label).join('、')}
      style={{ marginLeft: 6 }}
    >
      {affected ? '受规则变化影响' : '规则已变 · 结果不变'}
    </span>
  );
};
