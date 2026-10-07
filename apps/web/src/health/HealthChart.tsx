// GPU / VRAM の推移 (the place of the Monitoring board's 温度の推移): three lines in
// percent (GPU utilization, VRAM used and reserved of the total) on a recessive
// grid, the latest value labelled at the end of each line like the board, a
// tooltip on hover, and the same numbers as a table (表で見る: the way to read
// every value without a pointer), so no value is conveyed by color alone.
import { useEffect, useRef, useState } from "react";
import { useI18n } from "../i18n";
import type { PercentPoint } from "./model";

export const CHART_SERIES = ["gpu", "vramUsed", "vramReserved"] as const;
export type ChartSeries = (typeof CHART_SERIES)[number];

/** One time bucket: each series in percent (0-100), null where not recorded. */
type ChartPoint = PercentPoint;

const HEIGHT = 210;
const PLOT_TOP = 10;
const BASELINE = 186;
const AXIS_X = 34;
/** Room on the right for the end labels (series name and latest value). */
const END_LABELS = 86;
const TOOLTIP_W = 156;
const TOOLTIP_H = 82;
const TICKS = [0, 25, 50, 75, 100];

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

function percent(value: number | null): string {
  return value === null ? "—" : `${Math.round(value)}%`;
}

/** The axis label of a time: HH:MM within a day, M/D for longer periods. */
function useTimeLabel(long: boolean): (iso: string) => string {
  const { locale } = useI18n();
  return (iso) => {
    const date = new Date(iso);
    if (Number.isNaN(date.getTime())) return iso;
    return new Intl.DateTimeFormat(locale === "ja" ? "ja-JP" : "en-US", {
      ...(long ? { month: "numeric", day: "numeric" } : {}),
      hour: "2-digit",
      minute: "2-digit",
    }).format(date);
  };
}

function useSeriesLabel(): (series: ChartSeries) => string {
  const { t } = useI18n();
  return (series) => t(`health.chart.${series}`);
}

export function HealthChart({
  points,
  label,
  long,
}: {
  points: readonly ChartPoint[];
  label: string;
  /** Whether the period spans days (the labels then carry the date). */
  long: boolean;
}) {
  const { ref, width: measured } = useWidth(700);
  const width = Math.max(260, measured);
  const timeLabel = useTimeLabel(long);
  const seriesLabel = useSeriesLabel();
  const [active, setActive] = useState<number | null>(null);
  const plotRight = width - END_LABELS;
  const span = Math.max(1, points.length - 1);
  const xOf = (index: number) => AXIS_X + ((plotRight - AXIS_X) * index) / span;
  const yOf = (value: number) =>
    BASELINE - ((BASELINE - PLOT_TOP) * Math.min(100, Math.max(0, value))) / 100;

  function path(series: ChartSeries): string {
    let d = "";
    let pen = false;
    points.forEach((point, index) => {
      const value = point[series];
      if (value === null) {
        pen = false;
        return;
      }
      d += `${pen ? "L" : "M"} ${xOf(index).toFixed(1)} ${yOf(value).toFixed(1)} `;
      pen = true;
    });
    return d.trim();
  }

  function latest(series: ChartSeries): { index: number; value: number } | null {
    for (let index = points.length - 1; index >= 0; index--) {
      const value = points[index]?.[series];
      if (value !== null && value !== undefined) return { index, value };
    }
    return null;
  }

  // About five time labels, three on a phone-wide chart.
  const labelEvery = Math.max(1, Math.ceil(points.length / (width < 480 ? 2 : 4)));
  const onMove = (clientX: number, element: SVGSVGElement) => {
    const box = element.getBoundingClientRect();
    const x = ((clientX - box.left) / Math.max(1, box.width)) * width;
    const index = Math.round(((x - AXIS_X) / Math.max(1, plotRight - AXIS_X)) * span);
    setActive(Math.min(points.length - 1, Math.max(0, index)));
  };

  // End labels sit at their line's latest value, pushed apart when they overlap.
  const ends = CHART_SERIES.flatMap((series) => {
    const found = latest(series);
    return found ? [{ series, ...found, y: yOf(found.value) }] : [];
  }).sort((a, b) => a.y - b.y);
  for (let index = 1; index < ends.length; index++) {
    const previous = ends[index - 1];
    const current = ends[index];
    if (previous && current && current.y - previous.y < 26) current.y = previous.y + 26;
  }

  const current = active === null ? null : points[active];
  let tooltipX = 0;
  if (active !== null) {
    const x = xOf(active);
    tooltipX = x - TOOLTIP_W - 12;
    if (tooltipX < AXIS_X) tooltipX = x + 12;
    tooltipX = Math.min(tooltipX, width - TOOLTIP_W - 1);
  }

  return (
    <div className="health-chart" ref={ref}>
      <svg
        className="chart-svg"
        width={width}
        height={HEIGHT}
        viewBox={`0 0 ${width} ${HEIGHT}`}
        role="img"
        aria-label={label}
        onMouseMove={(event) => onMove(event.clientX, event.currentTarget)}
        onMouseLeave={() => setActive(null)}
      >
        {TICKS.map((value) => (
          <g key={value}>
            <line
              className="chart-grid"
              x1={AXIS_X}
              y1={yOf(value)}
              x2={plotRight}
              y2={yOf(value)}
            />
            <text className="chart-axis" x={AXIS_X - 7} y={yOf(value) + 3.5} textAnchor="end">
              {value}
            </text>
          </g>
        ))}
        {points.map((point, index) =>
          index === 0 ||
          index === points.length - 1 ||
          (index % labelEvery === 0 && points.length - 1 - index >= labelEvery / 2) ? (
            <text
              key={point.at}
              className="chart-axis"
              x={xOf(index)}
              y={HEIGHT - 6}
              textAnchor={index === 0 ? "start" : index === points.length - 1 ? "end" : "middle"}
            >
              {timeLabel(point.at)}
            </text>
          ) : null,
        )}
        {CHART_SERIES.map((series) => (
          <path key={series} className={`health-line line-${series}`} d={path(series)} />
        ))}
        {ends.map((end) => (
          <g key={end.series}>
            <circle
              className={`health-dot dot-${end.series}`}
              cx={xOf(end.index)}
              cy={yOf(end.value)}
              r={4.5}
            />
            <text className="chart-end-name" x={plotRight + 10} y={end.y - 2}>
              {seriesLabel(end.series)}
            </text>
            <text className="chart-axis" x={plotRight + 10} y={end.y + 12}>
              {percent(end.value)}
            </text>
          </g>
        ))}
        {current && active !== null && (
          <g pointerEvents="none">
            <line
              className="chart-cursor"
              x1={xOf(active)}
              y1={PLOT_TOP}
              x2={xOf(active)}
              y2={BASELINE}
            />
            {CHART_SERIES.map((series) => {
              const value = current[series];
              return value === null ? null : (
                <circle
                  key={series}
                  className={`health-dot dot-${series}`}
                  cx={xOf(active)}
                  cy={yOf(value)}
                  r={4}
                />
              );
            })}
            <rect
              className="chart-tooltip-box"
              x={tooltipX}
              y={14}
              width={TOOLTIP_W}
              height={TOOLTIP_H}
              rx={9}
            />
            <text className="chart-tooltip-date" x={tooltipX + 12} y={32}>
              {timeLabel(current.at)}
            </text>
            {CHART_SERIES.map((series, row) => (
              <g key={series}>
                <rect
                  className={`swatch-${series}`}
                  x={tooltipX + 12}
                  y={42 + row * 16}
                  width={8}
                  height={8}
                  rx={2}
                />
                <text className="chart-tooltip-label" x={tooltipX + 24} y={50 + row * 16}>
                  {seriesLabel(series)}
                </text>
                <text
                  className="chart-tooltip-value"
                  x={tooltipX + TOOLTIP_W - 10}
                  y={50 + row * 16}
                  textAnchor="end"
                >
                  {percent(current[series])}
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
export function HealthTable({ points, long }: { points: readonly ChartPoint[]; long: boolean }) {
  const { t } = useI18n();
  const timeLabel = useTimeLabel(long);
  const seriesLabel = useSeriesLabel();
  return (
    <div className="health-table-wrap">
      <table className="usage-table">
        <caption className="visually-hidden">{t("health.chart.title")}</caption>
        <thead>
          <tr>
            <th scope="col">{t("health.chart.time")}</th>
            {CHART_SERIES.map((series) => (
              <th key={series} scope="col">
                {seriesLabel(series)}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {points.map((point) => (
            <tr key={point.at}>
              <th scope="row">{timeLabel(point.at)}</th>
              {CHART_SERIES.map((series) => (
                <td key={series}>{percent(point[series])}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
