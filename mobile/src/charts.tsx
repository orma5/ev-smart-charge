// web.py's two charts, drawn from the same numbers with the same geometry.

import { View } from 'react-native';
import { Line, Rect, Svg } from 'react-native-svg';

import { StripSlot } from './api';
import { shortDay } from './format';
import { usePalette } from './theme';
import { T } from './ui';

/** Savings per day across the window, one bar per day that had a session. */
export function DailySavingsChart({ data }: { data: { day: string; savings: number }[] }) {
  const c = usePalette();
  const width = 760;
  const height = 120;
  const padBottom = 1;
  const padTop = 8;
  const plot = height - padBottom - padTop;
  const top = Math.max(...data.map((d) => d.savings), 0.01);
  const step = width / data.length;
  const bar = Math.max(2, Math.min(step - 3, 26));

  return (
    <View style={{ marginTop: 16 }}>
      {/* Stretched to its box rather than scaled, so bars keep their height on
          a narrow screen. A zero line, because a day that saved nothing is a
          real outcome and should read as zero rather than as missing. */}
      <Svg
        width="100%"
        height={72}
        viewBox={`0 0 ${width} ${height}`}
        preserveAspectRatio="none"
        accessibilityLabel="Savings per day in kronor"
      >
        <Line
          x1={0}
          y1={padTop + plot}
          x2={width}
          y2={padTop + plot}
          stroke={c.ink}
          vectorEffect="non-scaling-stroke"
        />
        {data.map((d, index) => {
          const tall = Math.max(1, (plot * d.savings) / top);
          return (
            <Rect
              key={d.day}
              x={index * step + (step - bar) / 2}
              y={padTop + plot - tall}
              width={bar}
              height={tall}
              rx={2}
              fill={c.accent}
            />
          );
        })}
      </Svg>
      <View style={{ flexDirection: 'row', justifyContent: 'space-between', marginTop: 4 }}>
        <T muted style={{ fontSize: 12 }}>{shortDay(data[0].day)}</T>
        {data.length > 1 && <T muted style={{ fontSize: 12 }}>{shortDay(data[data.length - 1].day)}</T>}
      </View>
    </View>
  );
}

/**
 * One session as a price strip: a bar per slot, highlighted where it charged.
 * The cheap slots are visibly the short ones, and the highlights should sit on
 * them.
 */
export function PriceStrip({ strip }: { strip: StripSlot[] }) {
  const c = usePalette();
  const width = 300;
  const height = 44;
  const top = Math.max(...strip.flatMap((s) => (s.price == null ? [] : [s.price])), 0.01);
  const step = width / strip.length;
  const bar = Math.max(1, step - 0.6);

  return (
    <Svg
      width="100%"
      height={height}
      viewBox={`0 0 ${width} ${height}`}
      preserveAspectRatio="none"
      style={{ marginTop: 10 }}
      accessibilityLabel="Price per slot, with charging slots highlighted"
    >
      {strip.map((s, index) => {
        if (s.price == null) {
          return null;
        }
        const tall = Math.max(1, (height * s.price) / top);
        return (
          <Rect
            key={s.slot}
            x={index * step}
            y={height - tall}
            width={bar}
            height={tall}
            fill={s.charged ? c.accent : c.rule}
          />
        );
      })}
    </Svg>
  );
}
