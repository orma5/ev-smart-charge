import { Tabs } from 'expo-router';
import { StatusBar } from 'expo-status-bar';

import { usePalette } from '../src/theme';
import { Icon } from '../src/ui';

export default function Layout() {
  const c = usePalette();
  // The icons are SVG markup and need a plain colour string, which the tab
  // bar's own `color` is not typed as.
  const tint = (focused: boolean) => (focused ? c.ink : c.muted);
  return (
    <>
      <StatusBar style="auto" />
      <Tabs
        screenOptions={{
          headerShown: false,
          tabBarActiveTintColor: c.ink,
          tabBarInactiveTintColor: c.muted,
          tabBarStyle: { backgroundColor: c.surface, borderTopColor: c.rule },
        }}
      >
        <Tabs.Screen
          name="index"
          options={{ title: 'Overview', tabBarIcon: ({ focused }) => <Icon name="home" color={tint(focused)} /> }}
        />
        <Tabs.Screen
          name="history"
          options={{ title: 'History', tabBarIcon: ({ focused }) => <Icon name="history" color={tint(focused)} /> }}
        />
        <Tabs.Screen
          name="settings"
          options={{ title: 'Settings', tabBarIcon: ({ focused }) => <Icon name="tune" color={tint(focused)} /> }}
        />
      </Tabs>
    </>
  );
}
