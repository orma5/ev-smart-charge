import { ReactNode, useState } from 'react';
import {
  ActivityIndicator,
  Pressable,
  RefreshControl,
  ScrollView,
  StyleSheet,
  Text,
  TextProps,
  View,
} from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';
import { SvgXml } from 'react-native-svg';

import { kr } from './format';
import { usePalette } from './theme';

// web.py's ICONS, copied verbatim so the app draws in the same hand as the web
// UI. Which icon a decision gets is the server's call (decision_icon).
const ICONS: Record<string, string> = {
  power_off: '<path d="M9 3v4M15 3v4M7 7h10v4a5 5 0 0 1-10 0zM12 16v5"/>',
  wrong_location: '<path d="M4 11.5 12 5l8 6.5M6 10v10h12V10M3 3l18 18"/>',
  battery_full: '<rect x="2.5" y="7" width="17" height="10" rx="2"/><path d="M22 10.5v3M7 12l2.5 2.5 5-5"/>',
  pause_circle: '<circle cx="12" cy="12" r="9"/><path d="M10 9v6M14 9v6"/>',
  timer: '<circle cx="12" cy="13.5" r="7.5"/><path d="M12 10v3.5l2.5 1.5M9.5 2.5h5"/>',
  bolt: '<path d="M13 2.5 4.5 13.5H11l-1 8 8.5-11H12z"/>',
  hourglass_top: '<path d="M6 3h12M6 21h12M8 3v3.5l4 5.5 4-5.5V3M8 21v-3.5l4-5.5 4 5.5V21"/>',
  money_off: '<path d="M20.5 12.5l-8 8a1.5 1.5 0 0 1-2.1 0l-6.9-6.9V3.5h10.1l6.9 6.9a1.5 1.5 0 0 1 0 2.1zM3 3l18 18"/>',
  error: '<circle cx="12" cy="12" r="9"/><path d="M12 7.5V13M12 16.5v.01"/>',
  help: '<circle cx="12" cy="12" r="9"/><path d="M9.5 9.5a2.5 2.5 0 1 1 3.4 2.3c-.6.3-.9.8-.9 1.4v.3M12 16.5v.01"/>',
  warning: '<path d="M12 3.5 21.5 20h-19zM12 10v4.5M12 17.5v.01"/>',
  info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v5.5M12 7.5v.01"/>',
  check: '<path d="M5 12.5l4.5 4.5L19 7.5"/>',
  home: '<path d="M3 12l9-8 9 8"/><path d="M5 10v10h14V10"/>',
  history: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
  tune:
    '<path d="M4 7h16M4 12h16M4 17h16"/><circle class="knob" cx="9" cy="7" r="2"/>' +
    '<circle class="knob" cx="15" cy="12" r="2"/><circle class="knob" cx="8" cy="17" r="2"/>',
};

export function Icon({ name, color, size = 22 }: { name: string; color: string; size?: number }) {
  const c = usePalette();
  // The sliders' knobs cover the lines they sit on, as the web tab bar does.
  const body = (ICONS[name] ?? ICONS.help).replace(/class="knob"/g, `fill="${c.surface}"`);
  const xml =
    `<svg viewBox="0 0 24 24" fill="none" stroke="${color}" stroke-width="1.9"` +
    ` stroke-linecap="round" stroke-linejoin="round">${body}</svg>`;
  return <SvgXml xml={xml} width={size} height={size} />;
}

/** Text in the palette's ink, or its muted grey. */
export function T({ muted, style, ...props }: TextProps & { muted?: boolean }) {
  const c = usePalette();
  return <Text {...props} style={[{ color: muted ? c.muted : c.ink, fontSize: 16 }, style]} />;
}

export function Screen({
  children,
  onRefresh,
}: {
  children: ReactNode;
  onRefresh?: () => Promise<void>;
}) {
  const c = usePalette();
  const [refreshing, setRefreshing] = useState(false);

  const pull = async () => {
    setRefreshing(true);
    await onRefresh?.();
    setRefreshing(false);
  };

  return (
    <SafeAreaView edges={['top']} style={{ flex: 1, backgroundColor: c.bg }}>
      <ScrollView
        contentContainerStyle={styles.screen}
        keyboardShouldPersistTaps="handled"
        automaticallyAdjustKeyboardInsets
        refreshControl={
          onRefresh && (
            <RefreshControl refreshing={refreshing} onRefresh={pull} tintColor={c.muted} colors={[c.accent]} />
          )
        }
      >
        {children}
      </ScrollView>
    </SafeAreaView>
  );
}

/** The first load: a spinner, or why there is nothing to show. */
export function Loading({ error, onRefresh }: { error: string | null; onRefresh: () => Promise<void> }) {
  const c = usePalette();
  return (
    <Screen onRefresh={onRefresh}>
      {error ? (
        <Banner kind="error">{error}</Banner>
      ) : (
        <ActivityIndicator color={c.muted} style={{ marginTop: 40 }} />
      )}
    </Screen>
  );
}

export function Head({ children }: { children: ReactNode }) {
  return <View style={styles.head}>{children}</View>;
}

export function Banner({ kind, children }: { kind: 'error' | 'warn' | 'notice'; children: ReactNode }) {
  const c = usePalette();
  const look = {
    error: { icon: 'error', color: c.error, box: { backgroundColor: c.errorBg } },
    warn: { icon: 'warning', color: c.warn, box: { backgroundColor: c.warnBg } },
    notice: { icon: 'check', color: c.ink, box: { backgroundColor: c.surface, borderWidth: 1, borderColor: c.rule } },
  }[kind];

  return (
    <View style={[styles.banner, look.box]}>
      <Icon name={look.icon} color={look.color} size={18} />
      <T style={{ flex: 1, color: look.color, fontSize: 14 }}>{children}</T>
    </View>
  );
}

/** The 7 / 30 / 90 day window, as underlined tabs. */
export function Period({ days, onChange }: { days: number; onChange: (days: number) => void }) {
  const c = usePalette();
  return (
    <View style={styles.period}>
      {[7, 30, 90].map((n) => (
        <Pressable
          key={n}
          onPress={() => onChange(n)}
          accessibilityRole="button"
          accessibilityLabel={`${n} days`}
          accessibilityState={{ selected: n === days }}
          style={[styles.periodItem, n === days && { borderBottomColor: c.ink }]}
        >
          <T style={{ fontSize: 15, fontWeight: '600', color: n === days ? c.ink : c.muted }}>{n}</T>
        </Pressable>
      ))}
    </View>
  );
}

/** A session's charge as a small battery bar: what it had, then what it added. */
export function ChargeBar({ start, end }: { start: number | null; end: number | null }) {
  const c = usePalette();
  if (start == null || end == null) {
    return null;
  }
  return (
    <View style={[styles.mini, { backgroundColor: c.track }]}>
      <View style={[styles.fill, { left: 0, width: `${start}%` as const, backgroundColor: c.ink }]} />
      {end > start && (
        <View
          style={[
            styles.fill,
            { left: `${start}%` as const, width: `${end - start}%` as const, backgroundColor: c.accent, opacity: 0.6 },
          ]}
        />
      )}
    </View>
  );
}

export function Saved({ amount }: { amount: number | null }) {
  return (
    <View style={{ alignItems: 'flex-end' }}>
      <T style={{ fontSize: 18, fontWeight: '700' }}>{kr(amount)}</T>
      <T muted style={{ fontSize: 12 }}>saved</T>
    </View>
  );
}

/** Folded-away explanation, like the web UI's <details>. */
export function About({ title, children }: { title: string; children: ReactNode }) {
  const c = usePalette();
  const [open, setOpen] = useState(false);
  return (
    <View style={{ marginTop: 8 }}>
      <Pressable
        onPress={() => setOpen(!open)}
        accessibilityRole="button"
        accessibilityState={{ expanded: open }}
        style={styles.summary}
      >
        <Icon name="info" color={c.accent} size={18} />
        <T style={{ color: c.accent, fontSize: 15, fontWeight: '600' }}>{title}</T>
      </Pressable>
      {open && children}
    </View>
  );
}

export function Para(props: TextProps) {
  return <T muted {...props} style={[{ fontSize: 14, marginBottom: 10 }, props.style]} />;
}

export const common = StyleSheet.create({
  title: { fontSize: 22, fontWeight: '700' },
  detail: { fontSize: 13 },
  empty: { fontSize: 15 },
  when: { fontSize: 15, fontWeight: '600' },
  row: {
    flexDirection: 'row',
    justifyContent: 'space-between',
    alignItems: 'center',
    gap: 16,
    paddingVertical: 14,
    borderTopWidth: 1,
  },
  item: { paddingVertical: 14, borderTopWidth: 1 },
});

const styles = StyleSheet.create({
  screen: { padding: 20, paddingBottom: 40, gap: 36 },
  head: { flexDirection: 'row', justifyContent: 'space-between', alignItems: 'center', gap: 12, minHeight: 44 },
  banner: { flexDirection: 'row', alignItems: 'flex-start', gap: 10, marginTop: 14, padding: 12, borderRadius: 10 },
  period: { flexDirection: 'row', gap: 2 },
  periodItem: {
    minWidth: 44,
    height: 44,
    alignItems: 'center',
    justifyContent: 'center',
    borderBottomWidth: 3,
    borderBottomColor: 'transparent',
  },
  mini: { width: 140, height: 8, marginVertical: 6, borderRadius: 4, overflow: 'hidden' },
  fill: { position: 'absolute', top: 0, bottom: 0 },
  summary: { flexDirection: 'row', alignItems: 'center', gap: 6, minHeight: 44 },
});
