import { useColorScheme } from 'react-native';

// base.html's tokens. Dark is its own palette - a spruce night rather than the
// light one inverted.
const light = {
  bg: '#eef2f0',
  surface: '#fbfcfb',
  ink: '#10302a',
  muted: '#5d6f69',
  rule: '#c6d3cd',
  track: '#d6e0db',
  accent: '#2f6f4f',
  stripe: '#c4e0d0',
  thumb: '#ffffff',
  error: '#a3261d',
  errorBg: '#f6dcd8',
  warn: '#6e4a00',
  warnBg: '#f3e4bf',
};

export type Palette = typeof light;

const dark: Palette = {
  bg: '#0e1714',
  surface: '#15211d',
  ink: '#e3ebe7',
  muted: '#93a59e',
  rule: '#2a3934',
  track: '#22302b',
  accent: '#74c69b',
  stripe: '#24402f',
  thumb: '#e3ebe7',
  error: '#f2b8b0',
  errorBg: '#4a1d18',
  warn: '#eac46e',
  warnBg: '#3d3113',
};

export function usePalette(): Palette {
  return useColorScheme() === 'dark' ? dark : light;
}
