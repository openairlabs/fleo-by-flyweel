import React, {useEffect, useState} from 'react';
import {Box, Text, useInput} from 'ink';

import {useTheme} from './ui/theme-provider.js';
import {useLayout} from '../lib/useTerminalSize.js';
import type {ProgressRow, SetupCheck} from '../types.js';

export const StatusMessage = ({tone = 'info', children}: {tone?: 'info' | 'success' | 'warning' | 'error'; children: React.ReactNode}) => {
  const theme = useTheme();
  const color = tone === 'success' ? theme.colors.success : tone === 'warning' ? theme.colors.warning : tone === 'error' ? theme.colors.error : theme.colors.info;
  const label = tone === 'success' ? '[OK]' : tone === 'warning' ? '[!]' : tone === 'error' ? '[ERR]' : '[i]';
  return (
    <Box>
      <Text color={color} bold>
        {label}{' '}
      </Text>
      {/* Status lines stay single-line so stacked notices keep a fixed height budget. */}
      <Text wrap="truncate-end">{children}</Text>
    </Box>
  );
};

export const Panel = ({
  title,
  children,
  grow = true,
}: {
  title?: string;
  children: React.ReactNode;
  grow?: boolean;
}) => {
  const theme = useTheme();
  return (
    <Box
      flexDirection="column"
      flexGrow={grow ? 1 : 0}
      borderStyle={theme.border.style}
      borderColor={theme.border.color}
      paddingX={1}
    >
      {title ? (
        <Text color={theme.colors.primary} bold>
          {title}
        </Text>
      ) : null}
      {children}
    </Box>
  );
};

export const KeyValue = ({items}: {items: Array<[string, React.ReactNode]>}) => {
  const theme = useTheme();
  const {columns} = useLayout();
  const labelWidth = columns < 50 ? 10 : 22;
  return (
    <Box flexDirection="column">
      {items.map(([key, value]) => (
        <Box key={key}>
          <Box width={labelWidth} flexShrink={0}>
            <Text color={theme.colors.mutedForeground}>{key}</Text>
          </Box>
          <Text wrap="truncate-end">{value}</Text>
        </Box>
      ))}
    </Box>
  );
};

const statusColor = (status: ProgressRow['status'], theme: ReturnType<typeof useTheme>) => {
  if (status === 'complete') return theme.colors.success;
  if (status === 'warning') return theme.colors.warning;
  if (status === 'error') return theme.colors.error;
  if (status === 'running') return theme.colors.info;
  return theme.colors.mutedForeground;
};

const STATUS_GLYPH: Record<ProgressRow['status'], string> = {
  pending: '·',
  running: '▸',
  complete: '✓',
  warning: '!',
  error: '✗',
  skipped: '·',
};

export const MultiProgress = ({rows}: {rows: ProgressRow[]}) => {
  const theme = useTheme();
  const visibleRows = rows.slice(-12);
  return (
    <Box flexDirection="column">
      {visibleRows.map(row => (
        <Box key={row.id}>
          <Box width={2}>
            <Text color={statusColor(row.status, theme)}>{STATUS_GLYPH[row.status]}</Text>
          </Box>
          <Box width={14}>
            <Text color={theme.colors.mutedForeground}>{row.phase}</Text>
          </Box>
          <Box width={24}>
            <Text>{row.label.slice(0, 22)}</Text>
          </Box>
          <Text color={theme.colors.mutedForeground} wrap="truncate-end">
            {row.detail ?? row.agent ?? ''}
          </Text>
        </Box>
      ))}
      {visibleRows.length === 0 ? <Text color={theme.colors.mutedForeground}>Waiting for the first progress event…</Text> : null}
    </Box>
  );
};

export const SetupFlow = ({checks}: {checks: SetupCheck[]}) => {
  const theme = useTheme();
  const {columns} = useLayout();
  const labelWidth = columns < 64 ? 18 : 26;
  return (
    <Box flexDirection="column">
      {checks.map(check => (
        <Box key={check.id}>
          <Box width={8} flexShrink={0}>
            <Text color={check.ok ? theme.colors.success : check.required ? theme.colors.error : theme.colors.warning}>
              {check.ok ? '[OK]' : check.required ? '[MISS]' : '[WARN]'}
            </Text>
          </Box>
          <Box width={labelWidth} flexShrink={0}>
            <Text wrap="truncate-end">{check.label}</Text>
          </Box>
          <Text dimColor wrap="truncate-end">
            {check.detail}
          </Text>
        </Box>
      ))}
    </Box>
  );
};

// Pure windowing math for list viewports: a slice of `span` items centered on the
// focused index, clamped to the list bounds. Exported for OptionList and its tests.
export const windowRange = (length: number, focusIndex: number, span: number): {start: number; end: number} => {
  const safeSpan = Math.max(1, Math.floor(span));
  if (length <= safeSpan) return {start: 0, end: Math.max(0, length)};
  const focus = Math.max(0, Math.min(length - 1, focusIndex));
  const start = Math.max(0, Math.min(focus - Math.floor(safeSpan / 2), length - safeSpan));
  return {start, end: start + safeSpan};
};

// Controlled selectable list that keeps its highlight where the caller says (initialIndex),
// unlike @inkjs/ui Select which always resets focus to the first option on mount. Used for
// long-lived menus the user returns to repeatedly (e.g. the settings list). When `viewport`
// is set and the options exceed it, a window centered on the focused option renders instead,
// with dim "… N more" markers top/bottom; total height never exceeds `viewport` lines.
export const OptionList = ({
  options,
  initialIndex = 0,
  onSelect,
  onFocusChange,
  isActive = true,
  viewport,
}: {
  options: Array<{value: string; label: string}>;
  initialIndex?: number;
  onSelect: (value: string, index: number) => void;
  // Fired whenever the clamped focus index changes (and on mount). Callers MUST store
  // only primitives from it (e.g. the index number) — writing back an object/array each
  // call would change this component's `options` identity and spin a re-render loop.
  onFocusChange?: (value: string, index: number) => void;
  isActive?: boolean;
  viewport?: number;
}) => {
  const theme = useTheme();
  const clamp = (index: number) => Math.max(0, Math.min(options.length - 1, index));
  const [index, setIndex] = useState(clamp(initialIndex));

  useInput(
    (_input, key) => {
      if (key.upArrow) setIndex(current => clamp(current - 1));
      if (key.downArrow) setIndex(current => clamp(current + 1));
      if (key.return) {
        const option = options[clamp(index)];
        if (option) onSelect(option.value, clamp(index));
      }
    },
    {isActive},
  );

  const focus = clamp(index);

  // Report the focused row to the caller. Keyed on the clamped index + options identity
  // so it settles after clamping and re-fires when a fresh option set lands.
  useEffect(() => {
    onFocusChange?.(options[focus]?.value ?? '', focus);
  }, [focus, options, onFocusChange]);

  const windowed = viewport !== undefined && options.length > viewport;
  // Two marker slots come out of the viewport budget when the list is clipped.
  const {start, end} = windowed ? windowRange(options.length, focus, Math.max(1, viewport - 2)) : {start: 0, end: options.length};

  return (
    <Box flexDirection="column">
      {/* When windowed, both marker slots are ALWAYS rendered (blank when unneeded) so the
          list height stays exactly `viewport` at the scroll ends too — scrolling must never
          change the frame height. */}
      {windowed ? <Text dimColor>{start > 0 ? `  … ${start} more` : ' '}</Text> : null}
      {options.slice(start, end).map((option, sliceIndex) => {
        const optionIndex = start + sliceIndex;
        const focused = optionIndex === focus;
        return (
          <Box key={option.value}>
            <Text color={focused ? theme.colors.focusRing : undefined} wrap="truncate-end">
              {focused ? '› ' : '  '}
              {option.label}
            </Text>
          </Box>
        );
      })}
      {windowed ? <Text dimColor>{end < options.length ? `  … ${options.length - end} more` : ' '}</Text> : null}
    </Box>
  );
};

