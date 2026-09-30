// 日別のタスク実行数 (the design's Usage board): bars stacked Local / Codex /
// Claude with a recessive grid, a tooltip per day on hover or keyboard focus, and
// the same numbers as a table (表で見る), so no value is conveyed by color alone.
import { useEffect, useRef, useState } from "react";
import { useI18n } from "../i18n";
import { AGENT_KINDS, type AgentKind, type DailyTasks, longDay, shortDay } from "./model";

const HEIGHT = 196;
const PLOT_TOP = 10;
const BASELINE = 174;
const AXIS_X = 30;
const BAR_MAX = 26;
/** A 2px gap of the card's surface between stacked segments. */
const SEGMENT_GAP = 2;
const RADIUS = 4;
const TOOLTIP_W = 148;
const TOOLTIP_H = 86;

/** The design's upper bar ends: only the top segment is rounded. */
function topSegment(x: number, y: number, width: number, height: number): string {
  const r = Math.min(RADIUS, height, width / 2);
  const bottom = y + height;
  return [
    `M ${x} ${bottom}`,
    `L ${x} ${y + r}`,
    `Q ${x} ${y} ${x + r} ${y}`,
    `L ${x + width - r} ${y}`,
    `Q ${x + width} ${y} ${x + width} ${y + r}`,
    `L ${x + width} ${bottom}`,
    "Z",
  ].join(" ");
}

/** A round step so the grid has about 3 lines below the tallest bar (0, 10, 20 for 25). */
function gridStep(max: number): number {
  if (max <= 2) return 1;
  const rough = max / 2.5;
  const magnitude = 10 ** Math.floor(Math.log10(rough));
  for (const factor of [1, 2, 5, 10]) {
    if (factor * magnitude >= rough) return factor * magnitude;
  }
  return 10 * magnitude;
}

function total(day: DailyTasks): number {
  return day.local + day.codex + day.claude;
}

/** Width of the card's plot area (the fallback where layout is not measured, e.g. jsdom). */
function useWidth(fallback: number) {
  const ref = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(fallback);
  useEffect(() => {
    const element = ref.current;
    if (!element || typeof ResizeObserver !== "function") return;
    const observer = new ResizeObserver(([entry]) => {
      if (entry && entry.contentRect.width > 0) setWidth(Math.floor(entry.contentRect.width));
    });
    observer.observe(element);
    return () => observer.disconnect();
  }, []);
  return { ref, width };
}

export function UsageChart({ days }: { days: readonly DailyTasks[] }) {
  const { t } = useI18n();
  const { ref, width } = useWidth(712);
  const [active, setActive] = useState<number | null>(null);

  const max = Math.max(1, ...days.map(total));
  const step = gridStep(max);
  // Like the design, the tallest bar nearly reaches the top; lines stay below it.
  const scale = (BASELINE - PLOT_TOP) / max;
  const slot = days.length > 0 ? (width - AXIS_X) / days.length : 0;
  const barWidth = Math.max(4, Math.min(BAR_MAX, slot * 0.55));
  // The design labels the first day, every 4th and the last.
  const labelEvery = Math.max(1, Math.ceil(days.length / 4));
  const ticks: number[] = [];
  for (let value = 0; value <= max; value += step) ticks.push(value);

  const current = active === null ? null : days[active];
  let tooltipX = 0;
  if (active !== null) {
    const center = AXIS_X + slot * active + slot / 2;
    tooltipX = center - TOOLTIP_W - barWidth;
    if (tooltipX < AXIS_X) tooltipX = center + barWidth;
    tooltipX = Math.min(tooltipX, width - TOOLTIP_W - 1);
  }

  return (
    <div className="usage-chart" ref={ref}>
      {/* A group, not an image: an image hides the focusable days from assistive technology. */}
      {/* biome-ignore lint/a11y/useSemanticElements: an SVG cannot be a <fieldset>; the group labels the chart's day controls */}
      <svg
        className="chart-svg"
        width={width}
        height={HEIGHT}
        viewBox={`0 0 ${width} ${HEIGHT}`}
        role="group"
        aria-label={t("usage.chart.label")}
        onMouseLeave={() => setActive(null)}
      >
        {ticks.map((value) => {
          const y = BASELINE - value * scale;
          return (
            <g key={value}>
              <line className="chart-grid" x1={AXIS_X} y1={y} x2={width} y2={y} />
              <text className="chart-axis" x={22} y={y + 3.5} textAnchor="end">
                {value}
              </text>
            </g>
          );
        })}
        {days.map((day, index) => {
          const x = AXIS_X + slot * index + (slot - barWidth) / 2;
          const present = AGENT_KINDS.filter((agent) => day[agent] > 0);
          let y = BASELINE;
          const segments = present.map((agent, order) => {
            const height = day[agent] * scale;
            const gap = order === 0 ? 0 : SEGMENT_GAP;
            const segmentTop = y - height;
            const drawn = Math.max(1, height - gap);
            const isTop = order === present.length - 1;
            const shape = isTop ? (
              <path
                key={agent}
                className={`series-${agent}`}
                d={topSegment(x, segmentTop, barWidth, drawn)}
              />
            ) : (
              <rect
                key={agent}
                className={`series-${agent}`}
                x={x}
                y={segmentTop}
                width={barWidth}
                height={drawn}
              />
            );
            y = segmentTop;
            return shape;
          });
          const showLabel = index === 0 || index === days.length - 1 || index % labelEvery === 0;
          return (
            // biome-ignore lint/a11y/useSemanticElements: an SVG group has no semantic element; the table view carries the same data
            <g
              key={day.date}
              role="button"
              tabIndex={0}
              aria-label={`${longDay(day.date)} ${AGENT_KINDS.map((agent) => `${t(`usage.agent.${agent}`)} ${day[agent]}`).join(", ")}`}
              onMouseEnter={() => setActive(index)}
              onFocus={() => setActive(index)}
              onBlur={() => setActive(null)}
              className="chart-day"
            >
              {/* The hit target is the whole column, larger than the bar. */}
              <rect
                className="chart-hit"
                x={AXIS_X + slot * index}
                y={PLOT_TOP}
                width={slot}
                height={BASELINE - PLOT_TOP}
              />
              {segments}
              {active === index && total(day) > 0 && (
                <rect
                  className="chart-focus"
                  x={x - 1}
                  y={BASELINE - total(day) * scale - 1}
                  width={barWidth + 2}
                  height={total(day) * scale + 1}
                  rx={RADIUS}
                />
              )}
              {showLabel && (
                <text
                  className="chart-axis"
                  x={x + barWidth / 2}
                  y={HEIGHT - 6}
                  textAnchor="middle"
                >
                  {shortDay(day.date)}
                </text>
              )}
            </g>
          );
        })}
        {current && (
          <g className="chart-tooltip" pointerEvents="none">
            <rect
              className="chart-tooltip-box"
              x={tooltipX}
              y={8}
              width={TOOLTIP_W}
              height={TOOLTIP_H}
              rx={9}
            />
            <text className="chart-tooltip-date" x={tooltipX + 12} y={26}>
              {longDay(current.date)}
            </text>
            {AGENT_KINDS.map((agent: AgentKind, row) => (
              <g key={agent}>
                <rect
                  className={`series-${agent}`}
                  x={tooltipX + 12}
                  y={36 + row * 17}
                  width={8}
                  height={8}
                  rx={2}
                />
                <text className="chart-tooltip-label" x={tooltipX + 24} y={44 + row * 17}>
                  {t(`usage.agent.${agent}`)}
                </text>
                <text
                  className="chart-tooltip-value"
                  x={tooltipX + TOOLTIP_W - 8}
                  y={44 + row * 17}
                  textAnchor="end"
                >
                  {current[agent]}
                </text>
              </g>
            ))}
          </g>
        )}
      </svg>
    </div>
  );
}

/** The same data as a table (表で見る). */
export function UsageTable({ days }: { days: readonly DailyTasks[] }) {
  const { t } = useI18n();
  return (
    <div className="usage-table-wrap">
      <table className="usage-table">
        <caption className="visually-hidden">{t("usage.chart.title")}</caption>
        <thead>
          <tr>
            <th scope="col">{t("usage.chart.date")}</th>
            {AGENT_KINDS.map((agent) => (
              <th key={agent} scope="col">
                {t(`usage.agent.${agent}`)}
              </th>
            ))}
            <th scope="col">{t("usage.chart.total")}</th>
          </tr>
        </thead>
        <tbody>
          {days.map((day) => (
            <tr key={day.date}>
              <th scope="row">{longDay(day.date)}</th>
              {AGENT_KINDS.map((agent) => (
                <td key={agent}>{day[agent]}</td>
              ))}
              <td>{total(day)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
