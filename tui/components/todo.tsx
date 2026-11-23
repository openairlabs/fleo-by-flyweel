import React from 'react';
import {Box, Text} from 'ink';

import {useTheme} from './ui/theme-provider.js';
import type {ProgressRow} from '../types.js';

// Rich CLI glyph vocabulary, shared verbatim so both frontends read the same.
export const GLYPHS: Record<ProgressRow['status'], string> = {
  pending: '○',
  running: '◐',
  complete: '●',
  warning: '!',
  error: '✗',
  skipped: '⊘',
};

export const SPINNER_FRAMES = ['⠋', '⠙', '⠹', '⠸', '⠼', '⠴', '⠦', '⠧', '⠇', '⠏'];

const BAR_WIDTH = 8;

export const renderBar = (current: number, total: number, width = BAR_WIDTH): string => {
  const safeTotal = Math.max(1, total);
  const filled = Math.max(0, Math.min(width, Math.round((current / safeTotal) * width)));
  return `[${'█'.repeat(filled)}${'░'.repeat(width - filled)}]`;
};

// Inline detail cell: `[████░░░░] 5/10 · 7 ins · "query…"` when the row carries
// bar fields, otherwise its latest detail message.
export const rowDetail = (row: ProgressRow): string => {
  if (typeof row.progressCurrent === 'number' && typeof row.progressTotal === 'number') {
    let detail = `${renderBar(row.progressCurrent, row.progressTotal)} ${row.progressCurrent}/${row.progressTotal}`;
    if (typeof row.insights === 'number') detail += ` · ${row.insights} ins`;
    if (row.query) detail += ` · "${row.query.length > 40 ? `${row.query.slice(0, 39)}…` : row.query}"`;
    if (row.detail) detail += ` · ${row.detail}`;
    return detail;
  }
  return row.detail ?? '';
};

export type TodoLine =
  | {kind: 'phase'; key: string; label: string; done: boolean}
  | {kind: 'task'; key: string; row: ProgressRow};

// Rows arrive already ordered by the run plan; emit a header whenever the
// phase changes so tasks read as grouped todo sections.
export const buildTodoLines = (
  rows: ProgressRow[],
  planPhases: Array<{phase: string; label: string}>,
): TodoLine[] => {
  const labels = new Map(planPhases.map(({phase, label}) => [phase, label]));
  const lines: TodoLine[] = [];
  let currentPhase: string | undefined;
  for (const row of rows) {
    if (row.phase !== currentPhase) {
      currentPhase = row.phase;
      const phaseRows = rows.filter(candidate => candidate.phase === currentPhase);
      lines.push({
        kind: 'phase',
        key: `phase:${currentPhase}`,
        label: labels.get(currentPhase) ?? currentPhase.charAt(0).toUpperCase() + currentPhase.slice(1),
        done: phaseRows.every(candidate => candidate.status !== 'pending' && candidate.status !== 'running'),
      });
    }
    lines.push({kind: 'task', key: row.id, row});
  }
  return lines;
};

export const activeLineIndex = (lines: TodoLine[]): number => {
  const running = lines.findIndex(line => line.kind === 'task' && line.row.status === 'running');
  if (running !== -1) return running;
  // Nothing running: focus the boundary between done and pending work.
  const firstPending = lines.findIndex(line => line.kind === 'task' && line.row.status === 'pending');
  if (firstPending !== -1) return firstPending;
  return Math.max(0, lines.length - 1);
};

export interface TodoWindow {
  lines: TodoLine[];
  hiddenAbove: number;
  hiddenBelow: number;
}

// Clip to `viewport` total lines (markers included), keeping the active line
// roughly centered so the eye never has to chase the moving row.
export const windowTodoLines = (lines: TodoLine[], viewport: number | undefined, active: number): TodoWindow => {
  if (!viewport || lines.length <= viewport) {
    return {lines, hiddenAbove: 0, hiddenBelow: 0};
  }
  const visible = Math.max(1, viewport - 2); // reserve marker lines
  let start = Math.max(0, active - Math.floor(visible / 2));
  start = Math.min(start, lines.length - visible);
  const end = start + visible;
  return {
    lines: lines.slice(start, end),
    hiddenAbove: start,
    hiddenBelow: lines.length - end,
  };
};

const TaskLine = ({row, tick}: {row: ProgressRow; tick: number}) => {
  const theme = useTheme();
  const color =
    row.status === 'complete'
      ? theme.colors.success
      : row.status === 'error'
        ? theme.colors.error
        : row.status === 'skipped' || row.status === 'warning'
          ? theme.colors.warning
          : row.status === 'running'
            ? theme.colors.accent
            : theme.colors.mutedForeground;
  const glyph = row.status === 'running' ? SPINNER_FRAMES[tick % SPINNER_FRAMES.length] : GLYPHS[row.status];
  const detail = rowDetail(row);
  const dimmed = row.status === 'pending' || row.status === 'skipped';
  return (
    <Box>
      <Box width={4} marginLeft={2}>
        <Text color={color} bold={row.status === 'running'}>
          {glyph}
        </Text>
      </Box>
      <Box width={20}>
        <Text color={dimmed ? theme.colors.mutedForeground : undefined} bold={row.status === 'running'} wrap="truncate-end">
          {row.label}
        </Text>
      </Box>
      <Box flexGrow={1}>
        <Text color={row.status === 'error' ? theme.colors.error : theme.colors.mutedForeground} wrap="truncate-end">
          {detail}
        </Text>
      </Box>
      {typeof row.elapsed === 'number' ? (
        <Box marginLeft={1}>
          <Text color={theme.colors.mutedForeground}>{row.elapsed.toFixed(1)}s</Text>
        </Box>
      ) : null}
    </Box>
  );
};

export const TodoList = ({
  rows,
  planPhases,
  viewport,
  tick = 0,
}: {
  rows: ProgressRow[];
  planPhases: Array<{phase: string; label: string}>;
  viewport?: number;
  tick?: number;
}) => {
  const theme = useTheme();
  if (rows.length === 0) {
    return <Text color={theme.colors.mutedForeground}>Waiting for the run plan…</Text>;
  }
  const lines = buildTodoLines(rows, planPhases);
  const {lines: visible, hiddenAbove, hiddenBelow} = windowTodoLines(lines, viewport, activeLineIndex(lines));
  return (
    <Box flexDirection="column">
      {hiddenAbove > 0 ? <Text color={theme.colors.mutedForeground}>  … {hiddenAbove} more</Text> : null}
      {visible.map(line =>
        line.kind === 'phase' ? (
          <Box key={line.key}>
            <Text color={line.done ? theme.colors.success : theme.colors.primary} bold>
              {line.label}
            </Text>
          </Box>
        ) : (
          <TaskLine key={line.key} row={line.row} tick={tick} />
        ),
      )}
      {hiddenBelow > 0 ? <Text color={theme.colors.mutedForeground}>  … {hiddenBelow} more</Text> : null}
    </Box>
  );
};
