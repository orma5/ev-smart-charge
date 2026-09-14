import { Link } from 'expo-router';
import { useCallback, useState } from 'react';
import { StyleSheet, View } from 'react-native';

import { getOverview } from '../src/api';
import { DailySavingsChart } from '../src/charts';
import { ago, kr, kwh, pct, when } from '../src/format';
import { usePalette } from '../src/theme';
import { About, Banner, ChargeBar, common, Head, Icon, Loading, Para, Period, Saved, Screen, T } from '../src/ui';
import { useApi } from '../src/useApi';

// Mirrors app.POLL_SECONDS: no point refreshing faster than the scheduler writes.
const REFRESH_MS = 4 * 60 * 1000;

export default function Overview() {
  const c = usePalette();
  const [days, setDays] = useState(30);
  const load = useCallback(() => getOverview(days), [days]);
  const { data, error, refresh } = useApi(load, REFRESH_MS);

  if (!data) {
    return <Loading error={error} onRefresh={refresh} />;
  }

  const { latest, reading, settings, totals } = data;
  // The battery comes from the last run that read the car, not the last run:
  // a tick that finds nothing on the home charger never asks the car.
  const battery = reading?.battery_percent ?? null;
  const target = reading?.target_percent || settings.charge_limit_percent;

  const note =
    latest &&
    [
      reading && reading.at !== latest.at ? `Battery as of ${when(reading.at)}.` : '',
      reading && reading.at === latest.at && battery != null && battery < target
        ? `${kwh(((target - battery) * settings.battery_capacity_kwh) / 100)} to go.`
        : '',
      `Checked ${ago(latest.at)}.`,
    ]
      .filter(Boolean)
      .join(' ');

  return (
    <Screen onRefresh={refresh}>
      <View>
        {error ? <Banner kind="error">{error}</Banner> : null}
        <Head>
          <T style={styles.brand}>EV Smart Charge</T>
          {settings.smart_charging_enabled ? (
            <View style={[styles.pill, { borderColor: c.rule }]}>
              <View style={[styles.dot, { backgroundColor: c.accent }]} />
              <T style={styles.pillText}>Smart charging on</T>
            </View>
          ) : (
            <View style={[styles.pill, { backgroundColor: c.warnBg, borderColor: 'transparent' }]}>
              <Icon name="warning" color={c.warn} size={16} />
              <T style={[styles.pillText, { color: c.warn }]}>Smart charging off</T>
            </View>
          )}
        </Head>
      </View>

      <View>
        {battery != null && (
          <>
            <View style={styles.socRow}>
              <T style={styles.soc}>{battery}%</T>
              <View style={styles.target}>
                <T muted style={{ fontSize: 13 }}>Target</T>
                <T style={styles.targetValue}>{target}%</T>
              </View>
            </View>
            <Battery percent={battery} target={target} />
          </>
        )}

        {latest ? (
          <>
            <View style={styles.status}>
              <Icon name={latest.decision_icon} color={latest.error ? c.error : c.ink} />
              <View style={{ flex: 1 }}>
                <T style={[styles.statusText, latest.error ? { color: c.error } : null]}>{latest.decision_label}</T>
                <T muted style={{ fontSize: 14 }}>{note}</T>
              </View>
            </View>
            {latest.error ? <Banner kind="error">{latest.error}</Banner> : null}
          </>
        ) : (
          <T muted style={common.empty}>No runs recorded yet. The scheduler writes one every four minutes.</T>
        )}
      </View>

      <View>
        <Head>
          <T style={common.title}>Last {days} days</T>
          <Period days={days} onChange={setDays} />
        </Head>

        {/* A receipt: what charging on plug-in would have cost, less what it did. */}
        <View style={{ marginTop: 14 }}>
          <View style={[styles.ledgerRow, { borderTopColor: c.rule }]}>
            <T muted>Charging on plug-in</T>
            <T muted>{kr(totals.baseline_cost)}</T>
          </View>
          <View style={[styles.ledgerRow, { borderTopColor: c.rule }]}>
            <T>Smart charging</T>
            <T>−{kr(totals.actual_cost)}</T>
          </View>
          <View style={[styles.total, { borderTopColor: c.ink }]}>
            <T style={styles.totalLabel}>Saved</T>
            <T style={[styles.totalValue, { color: c.accent }]}>{kr(totals.savings)}</T>
          </View>
        </View>
        <T muted style={{ fontSize: 14, marginTop: 10 }}>
          {kwh(totals.energy_kwh)} across {totals.sessions} session{totals.sessions === 1 ? '' : 's'}
        </T>

        {data.daily_savings.length > 0 ? (
          <DailySavingsChart data={data.daily_savings} />
        ) : (
          <T muted style={[common.empty, { marginTop: 14 }]}>
            Nothing to chart yet: no completed charging sessions in this window.
          </T>
        )}

        {totals.unpriced > 0 ? (
          <Banner kind="warn">
            {totals.unpriced} session{totals.unpriced === 1 ? ' is' : 's are'} left out of these totals: spot prices
            are missing for some of the slots involved.
          </Banner>
        ) : null}

        <About title="How this is calculated">
          <Para>
            The comparison is against charging flat out from the moment the cable went in until the car reached its
            target. Energy comes from the charger&apos;s own meter where it reported one; for sessions charged before
            the charger was wired in, it is estimated from the car&apos;s state of charge and the battery size in
            settings, so treat those as close rather than exact. Metered sessions read a little higher than estimated
            ones, because the meter counts the charging losses that never reach the battery — which is the figure you
            are actually billed for.
          </Para>
        </About>
      </View>

      {data.sessions.length > 0 && (
        <View>
          <Head>
            <T style={common.title}>Sessions</T>
            <Link href="/history" style={{ color: c.accent, fontSize: 16, fontWeight: '600' }}>
              All sessions
            </Link>
          </Head>
          {data.sessions.map((session) => (
            <View key={session.started_at} style={[common.row, { borderTopColor: c.rule }]}>
              <View style={{ flex: 1 }}>
                <T style={common.when}>{when(session.started_at)}</T>
                <ChargeBar start={session.start_percent} end={session.end_percent} />
                <T muted style={common.detail}>
                  {pct(session.start_percent)} to {pct(session.end_percent)}, {kwh(session.energy_kwh)} for{' '}
                  {kr(session.actual_cost)}
                </T>
              </View>
              <Saved amount={session.savings} />
            </View>
          ))}
        </View>
      )}
    </Screen>
  );
}

function Battery({ percent, target }: { percent: number; target: number }) {
  const c = usePalette();
  return (
    <View
      style={styles.battery}
      accessibilityRole="image"
      accessibilityLabel={`Battery at ${percent}%, target ${target}%`}
    >
      <View style={[styles.cell, { borderColor: c.ink }]}>
        <View style={styles.inner}>
          <View style={[styles.level, { left: 0, width: `${Math.min(percent, 100)}%` as const, backgroundColor: c.ink }]} />
          {/* What tonight's charge still has to add. */}
          {percent < target && (
            <View
              style={[
                styles.level,
                styles.toGo,
                {
                  left: `${percent}%` as const,
                  width: `${target - percent}%` as const,
                  backgroundColor: c.stripe,
                  borderColor: c.accent,
                },
              ]}
            />
          )}
        </View>
      </View>
      <View style={[styles.nub, { backgroundColor: c.ink }]} />
    </View>
  );
}

const styles = StyleSheet.create({
  brand: { fontSize: 17, fontWeight: '700' },
  pill: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: 8,
    height: 32,
    paddingHorizontal: 12,
    borderWidth: 1,
    borderRadius: 999,
  },
  dot: { width: 8, height: 8, borderRadius: 4 },
  pillText: { fontSize: 13, fontWeight: '500' },

  socRow: { flexDirection: 'row', justifyContent: 'space-between', alignItems: 'flex-end', gap: 12 },
  soc: { fontSize: 104, lineHeight: 104, fontWeight: '800', letterSpacing: -4, includeFontPadding: false },
  target: { alignItems: 'flex-end', paddingBottom: 12 },
  targetValue: { fontSize: 28, fontWeight: '700' },

  battery: { flexDirection: 'row', alignItems: 'center', gap: 3, marginTop: 14 },
  cell: { flexGrow: 1, height: 60, borderWidth: 3, borderRadius: 14 },
  inner: { position: 'absolute', top: 5, right: 5, bottom: 5, left: 5 },
  nub: { width: 7, height: 22, borderTopRightRadius: 4, borderBottomRightRadius: 4 },
  level: { position: 'absolute', top: 0, bottom: 0, borderRadius: 7 },
  toGo: { marginLeft: 3, borderWidth: 2 },

  status: { flexDirection: 'row', alignItems: 'flex-start', gap: 10, marginTop: 14 },
  statusText: { fontSize: 18, fontWeight: '600' },

  ledgerRow: {
    flexDirection: 'row',
    justifyContent: 'space-between',
    alignItems: 'baseline',
    gap: 12,
    paddingVertical: 10,
    borderTopWidth: 1,
  },
  total: {
    flexDirection: 'row',
    justifyContent: 'space-between',
    alignItems: 'baseline',
    gap: 12,
    paddingTop: 12,
    borderTopWidth: 3,
  },
  totalLabel: { fontSize: 18, fontWeight: '700' },
  totalValue: { fontSize: 38, fontWeight: '800' },
});
