import ReactECharts from "echarts-for-react";
import type { EChartsOption } from "echarts";

/** ECharts option objects are built inline per screen; the wrapper takes them structurally. */
export function Chart({ option, height = 220, className, onEvents }: {
  option: object; height?: number | string; className?: string; onEvents?: Record<string, (e: unknown) => void>;
}) {
  return (
    <ReactECharts
      option={option as EChartsOption}
      onEvents={onEvents}
      notMerge
      lazyUpdate
      style={{ height, width: "100%" }}
      className={className}
      opts={{ renderer: "canvas" }}
    />
  );
}
