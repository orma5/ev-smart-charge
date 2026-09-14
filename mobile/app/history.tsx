import { useCallback, useState } from 'react';
import { StyleSheet, View } from 'react-native';

import { getHistory, RecentRow } from '../src/api';
import { PriceStrip } from '../src/charts';
import { dayLabel, hhmm, kr, kwh, pct, weekday, when } from '../src/format';
import { usePalette } from '../src/theme';
import { Banner, ChargeBar, common, Head, Icon, Loading, Period, Saved, Screen, T } from '../src/ui';
import { useApi } from '../src/useApi';

export default function History() {
  const c = usePalette();
  const [days, setDays] = useState(30);
  const load = useCallback(() => getHistory(days), [days]);
  const { data, error, refresh } = useApi(load);

  if (!data) {
    return <Loading error={error} onRefresh={refresh} />;
  }

  return (
    <Screen onRefresh={refresh}>
      <View>
        {error ? <Banner kind="error">{error}</Banner> : null}
        <Head>
          <T style={common.title}>Charging sessions</T>
          <Period days={days} onChange={setDays} />
        </Head>
        <T muted style={{ fontSize: 14 }}>Last {days} days</T>

        <View style={{ marginTop: 14 }}>
          {data.sessions.length === 0 ? (
            <View style={[common.item, { borderTopColor: c.rule }]}>
              <T muted style={common.empty}>No sessions in the last {days} days.</T>
            </View>
          ) : (
            data.sessions.map((session) => (
              <View key={session.started_at} style={[common.item, { borderTopColor: c.rule }]}>
                <View style={styles.sessionHead}>
                  <View style={{ flex: 1 }}>
                    <T style={common.when}>{when(session.started_at)}</T>
                    <T muted style={common.detail}>Unplugged {when(session.ended_at)}</T>
                  </View>
                  <Saved amount={session.savings} />
                </View>
                <ChargeBar start={session.start_percent} end={session.end_percent} />
                <T muted style={common.detail}>
                  {pct(session.start_percent)} to {pct(session.end_percent)}, {kwh(session.energy_kwh)} for{' '}
                  {kr(session.actual_cost)} instead of {kr(session.baseline_cost)}
                </T>
                {session.strip ? <PriceStrip strip={session.strip} /> : null}
                {session.savings == null ? (
                  <Banner kind="warn">Not priced: spot prices are missing for some of the slots involved.</Banner>
                ) : null}
                {session.error_count > 0 ? (
                  <Banner kind="error">
                    {session.error_count} run{session.error_count === 1 ? '' : 's'} failed during this session.
                  </Banner>
                ) : null}
              </View>
            ))
          )}
        </View>
      </View>

      {/* The runs log: time, what it decided, battery. A heading per day, so
          the date is not repeated on every row. */}
      <View>
        <Head>
          <T style={common.title}>Recent runs</T>
        </Head>
        {data.recent.length === 0 ? (
          <T muted style={[common.empty, { marginTop: 14 }]}>Nothing recorded yet.</T>
        ) : (
          data.recent.map((row, index) => {
            const newDay = index === 0 || dayLabel(row.started_at) !== dayLabel(data.recent[index - 1].started_at);
            const detail = runDetail(row);
            return (
              <View key={`${row.started_at}-${index}`}>
                {newDay ? <T style={styles.day}>{dayLabel(row.started_at)}</T> : null}
                <View style={[styles.run, !newDay && { borderTopWidth: 1, borderTopColor: c.rule }]}>
                  <T muted style={styles.time}>{hhmm(row.started_at)}</T>
                  <View
                    style={[
                      styles.badge,
                      row.error
                        ? { backgroundColor: c.errorBg, borderColor: 'transparent' }
                        : { backgroundColor: c.surface, borderColor: c.rule },
                    ]}
                  >
                    <Icon name={row.decision_icon} color={row.error ? c.error : c.ink} size={20} />
                  </View>
                  <View style={{ flex: 1 }}>
                    <T style={styles.what}>{row.decision_label}</T>
                    {detail ? <T muted style={common.detail}>{detail}</T> : null}
                    {row.error ? <T style={[common.detail, { color: c.error }]}>{row.error}</T> : null}
                  </View>
                  <T>
                    {row.start_percent !== row.end_percent
                      ? `${pct(row.start_percent)} → ${pct(row.end_percent)}`
                      : pct(row.end_percent)}
                  </T>
                </View>
              </View>
            );
          })
        )}
      </View>
    </Screen>
  );
}

/** "to 02:30, 3 runs, CHARGING" - the same line the web history writes. */
function runDetail(row: RecentRow) {
  const parts = [];
  if (row.count > 1) {
    const sameDay = dayLabel(row.ended_at) === dayLabel(row.started_at);
    const end = sameDay ? hhmm(row.ended_at) : `${weekday(row.ended_at)} ${hhmm(row.ended_at)}`;
    parts.push(`to ${end}`, `${row.count} runs`);
  }
  if (row.charging_state) {
    parts.push(row.charging_state);
  }
  return parts.join(', ');
}

const styles = StyleSheet.create({
  sessionHead: { flexDirection: 'row', justifyContent: 'space-between', alignItems: 'flex-start', gap: 16 },
  day: { paddingTop: 20, paddingBottom: 6, fontSize: 14, fontWeight: '700' },
  run: { flexDirection: 'row', alignItems: 'center', gap: 12, paddingVertical: 10 },
  time: { width: 48 },
  badge: { width: 36, height: 36, borderRadius: 10, borderWidth: 1, alignItems: 'center', justifyContent: 'center' },
  what: { fontSize: 15, fontWeight: '600' },
});
