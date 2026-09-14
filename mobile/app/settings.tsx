import { useState } from 'react';
import { Pressable, StyleSheet, Switch, TextInput, View } from 'react-native';

import { getSettings, saveSettings, Settings, SettingsDraft } from '../src/api';
import { usePalette } from '../src/theme';
import { About, Banner, common, Head, Loading, Para, Screen, T } from '../src/ui';
import { explain, useApi } from '../src/useApi';

// Bounds and messages live on the server, which checks them for the web form
// too. The keyboards are all this side decides.
const FIELDS = [
  { name: 'departure_hour', label: 'Departure hour', hint: 'Ready by this hour, every day.', keyboard: 'number-pad' },
  { name: 'charger_speed_kw', label: 'Charger speed (kW)', hint: 'How fast the wallbox charges.', keyboard: 'decimal-pad' },
  {
    name: 'battery_capacity_kwh',
    label: 'Battery capacity (kWh)',
    hint: 'Plans charge time; also prices unmetered sessions.',
    keyboard: 'decimal-pad',
  },
  {
    name: 'charge_limit_percent',
    label: 'Charge limit fallback (%)',
    hint: 'Used only when the car reports no target.',
    keyboard: 'number-pad',
  },
] as const;

type Field = (typeof FIELDS)[number]['name'];

const toDraft = (settings: Settings): SettingsDraft => ({
  departure_hour: String(settings.departure_hour),
  charger_speed_kw: String(settings.charger_speed_kw),
  battery_capacity_kwh: String(settings.battery_capacity_kwh),
  charge_limit_percent: String(settings.charge_limit_percent),
  smart_charging_enabled: settings.smart_charging_enabled,
});

export default function SettingsScreen() {
  const c = usePalette();
  const { data, setData, error, refresh } = useApi(getSettings);
  // null until something is edited, so a refetch shows what the server has
  // without ever throwing away edits in progress.
  const [draft, setDraft] = useState<SettingsDraft | null>(null);
  const [problems, setProblems] = useState<Record<string, string>>({});
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);

  const current = draft ?? (data && toDraft(data));
  if (!current) {
    return <Loading error={error} onRefresh={refresh} />;
  }

  const edit = (next: SettingsDraft) => {
    setDraft(next);
    setSaved(false);
  };

  const setField = (name: Field, text: string) => {
    const next = { ...current };
    next[name] = text;
    edit(next);
  };

  const save = async () => {
    setSaving(true);
    setSaveError(null);
    try {
      const result = await saveSettings(current);
      if (result.ok) {
        setData(result.settings);
        setDraft(null);
        setProblems({});
        setSaved(true);
      } else {
        setProblems(result.problems);
      }
    } catch (e) {
      setSaveError(explain(e));
    } finally {
      setSaving(false);
    }
  };

  return (
    <Screen onRefresh={refresh}>
      <View>
        <Head>
          <T style={common.title}>Settings</T>
        </Head>

        {saved ? <Banner kind="notice">Saved. The next run picks these up within four minutes.</Banner> : null}
        {saveError ? <Banner kind="error">{saveError}</Banner> : null}

        {/* The one control with an immediate consequence: off means the car
            charges the moment it is plugged in. */}
        <View style={[styles.field, { borderBottomColor: c.rule }]}>
          <View style={styles.fieldRow}>
            <T style={styles.label}>Smart charging</T>
            <Switch
              value={current.smart_charging_enabled}
              onValueChange={(on) => edit({ ...current, smart_charging_enabled: on })}
              trackColor={{ false: c.track, true: c.accent }}
              thumbColor={c.thumb}
              ios_backgroundColor={c.track}
            />
          </View>
          <T muted style={styles.hint}>Off charges as soon as it is plugged in.</T>
        </View>

        {FIELDS.map((field) => (
          <View key={field.name} style={[styles.field, { borderBottomColor: c.rule }]}>
            <View style={styles.fieldRow}>
              <T style={styles.label}>{field.label}</T>
              <TextInput
                value={current[field.name]}
                onChangeText={(text) => setField(field.name, text)}
                keyboardType={field.keyboard}
                selectTextOnFocus
                accessibilityLabel={field.label}
                style={[
                  styles.input,
                  {
                    color: c.ink,
                    backgroundColor: c.surface,
                    borderColor: problems[field.name] ? c.error : c.rule,
                  },
                ]}
              />
            </View>
            <T muted style={styles.hint}>{field.hint}</T>
            {problems[field.name] ? (
              <T style={[styles.hint, { color: c.error, fontWeight: '500' }]}>{problems[field.name]}</T>
            ) : null}
          </View>
        ))}

        <Pressable
          onPress={save}
          disabled={saving}
          accessibilityRole="button"
          style={[styles.primary, { backgroundColor: c.ink, opacity: saving ? 0.6 : 1 }]}
        >
          <T style={[styles.primaryText, { color: c.bg }]}>{saving ? 'Saving…' : 'Save'}</T>
        </Pressable>

        <About title="What these do">
          <Para>
            <T style={styles.strong}>Smart charging</T> off means the car is left alone and charges as soon as it is
            plugged in. Nothing is recorded as saved while it is off.
          </Para>
          <Para>
            <T style={styles.strong}>Charger speed</T> decides how many 15-minute slots a given amount of charge needs,
            and prices the plug-in-and-go comparison the savings are measured against.
          </Para>
          <Para>
            <T style={styles.strong}>Battery capacity</T> turns a change in state of charge into kilowatt-hours, which
            decides how long a charge will take and therefore how many cheap slots it needs. For the savings figures it
            is now only a fallback: where the charger metered a session itself, that measurement is used instead, and
            it reads around a tenth higher because it counts the losses that never reach the battery.
          </Para>
          <Para>
            <T style={styles.strong}>Charge limit fallback</T> usually has no effect. The car reports its own target —
            the one set in the MyŠkoda app — and that wins whenever it is present. This is only used on the rare runs
            where the car reports no target at all.
          </Para>
          <Para>
            The price zone, the Skoda and Zaptec API details and the database connection are not here on purpose: they
            are facts about how this is deployed rather than preferences, and a text field that can break the car
            charging is not worth the convenience.
          </Para>
        </About>
      </View>
    </Screen>
  );
}

const styles = StyleSheet.create({
  field: { paddingVertical: 14, borderBottomWidth: 1, gap: 2 },
  fieldRow: { flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between', gap: 16 },
  label: { flex: 1, fontWeight: '600' },
  hint: { fontSize: 13 },
  input: {
    width: 104,
    height: 44,
    paddingHorizontal: 12,
    fontSize: 16,
    fontWeight: '600',
    textAlign: 'right',
    borderWidth: 1,
    borderRadius: 10,
  },
  primary: { height: 52, marginTop: 20, borderRadius: 12, alignItems: 'center', justifyContent: 'center' },
  primaryText: { fontSize: 16, fontWeight: '700' },
  strong: { fontSize: 14, fontWeight: '700' },
});
