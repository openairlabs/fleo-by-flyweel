import React from 'react';
import {render} from 'ink-testing-library';
import {describe, expect, it} from 'vitest';

import {activeLineIndex, buildTodoLines, renderBar, rowDetail, TodoList, windowTodoLines} from '../../tui/components/todo.js';
import {ThemeProvider} from '../../tui/components/ui/theme-provider.js';
import {createInitialState, reduceBridgeEvent} from '../../tui/state.js';
import type {AppState, BridgeEvent, PlanPhase, ProgressRow} from '../../tui/types.js';

const event = (patch: Partial<BridgeEvent> & {type: BridgeEvent['type']}): BridgeEvent => ({
  runId: 'run_test',
  timestamp: '2026-07-02T00:00:00Z',
  ...patch,
});

const PLAN: PlanPhase[] = [
  {phase: 'context', label: 'Loading Context', tasks: [{id: 'context:repo-context', label: 'Repo Context'}]},
  {
    phase: 'research',
    label: 'Research',
    tasks: [
      {id: 'research:serp-analysis', label: 'SERP Analysis'},
      {id: 'research:reddit-mining', label: 'Reddit Mining'},
    ],
  },
  {
    phase: 'refinement',
    label: 'Refinement',
    tasks: [
      {id: 'refinement:edit-pass', label: 'Edit Pass'},
      {id: 'refinement:review-gate', label: 'Review Gate'},
    ],
  },
];

const seeded = (): AppState =>
  reduceBridgeEvent(createInitialState(), event({type: 'run_plan', phase: 'setup', data: {plan: PLAN}}));

describe('run_plan seeding', () => {
  it('seeds one pending row per task, ordered and phase-grouped', () => {
    const state = seeded();
    expect(state.planPhases).toEqual([
      {phase: 'context', label: 'Loading Context'},
      {phase: 'research', label: 'Research'},
      {phase: 'refinement', label: 'Refinement'},
    ]);
    expect(state.progressRows.map(row => row.id)).toEqual([
      'task:context:repo-context',
      'task:research:serp-analysis',
      'task:research:reddit-mining',
      'task:refinement:edit-pass',
      'task:refinement:review-gate',
    ]);
    expect(state.progressRows.every(row => row.status === 'pending')).toBe(true);
  });

  it('ignores an empty plan payload', () => {
    const state = reduceBridgeEvent(createInitialState(), event({type: 'run_plan', data: {plan: []}}));
    expect(state.planPhases).toEqual([]);
    expect(state.progressRows).toEqual([]);
  });
});

describe('plan-aware task updates', () => {
  it('updates seeded rows in place from scoped taskIds', () => {
    let state = seeded();
    state = reduceBridgeEvent(
      state,
      event({
        type: 'task_progress',
        phase: 'research',
        taskId: 'research:serp-analysis',
        message: 'Perplexity sonar',
        data: {status: 'running', label: 'SERP Analysis'},
      }),
    );
    const row = state.progressRows.find(candidate => candidate.id === 'task:research:serp-analysis');
    expect(row?.status).toBe('running');
    expect(row?.detail).toBe('Perplexity sonar');
    expect(state.progressRows).toHaveLength(5);

    state = reduceBridgeEvent(
      state,
      event({
        type: 'task_progress',
        phase: 'research',
        taskId: 'research:serp-analysis',
        message: '12 questions',
        data: {status: 'complete', elapsed: 3.2},
      }),
    );
    const done = state.progressRows.find(candidate => candidate.id === 'task:research:serp-analysis');
    expect(done?.status).toBe('complete');
    expect(done?.elapsed).toBe(3.2);
  });

  it('maps engine failed status onto the error style', () => {
    let state = seeded();
    state = reduceBridgeEvent(
      state,
      event({type: 'task_progress', phase: 'refinement', taskId: 'refinement:edit-pass', data: {status: 'failed'}}),
    );
    expect(state.progressRows.find(row => row.id === 'task:refinement:edit-pass')?.status).toBe('error');
  });

  it('carries n/m bar fields and clears them on completion', () => {
    let state = seeded();
    state = reduceBridgeEvent(
      state,
      event({
        type: 'task_progress',
        phase: 'research',
        taskId: 'research:reddit-mining',
        data: {status: 'running', progressCurrent: 5, progressTotal: 10, insights: 7, query: 'best crm tools'},
      }),
    );
    let row = state.progressRows.find(candidate => candidate.id === 'task:research:reddit-mining');
    expect(row?.progressCurrent).toBe(5);
    expect(rowDetail(row as ProgressRow)).toContain('5/10');
    expect(rowDetail(row as ProgressRow)).toContain('7 ins');
    expect(rowDetail(row as ProgressRow)).toContain('"best crm tools"');

    state = reduceBridgeEvent(
      state,
      event({
        type: 'task_progress',
        phase: 'research',
        taskId: 'research:reddit-mining',
        message: '31 raw insights',
        data: {status: 'complete', elapsed: 42.1},
      }),
    );
    row = state.progressRows.find(candidate => candidate.id === 'task:research:reddit-mining');
    expect(row?.progressCurrent).toBeUndefined();
    expect(rowDetail(row as ProgressRow)).toBe('31 raw insights');
  });

  it('appends unknown scoped taskIds inside their phase group', () => {
    let state = seeded();
    state = reduceBridgeEvent(
      state,
      event({
        type: 'task_progress',
        phase: 'research',
        taskId: 'research:research-index-search',
        data: {status: 'running', label: 'Research Index Search'},
      }),
    );
    const ids = state.progressRows.map(row => row.id);
    expect(ids.indexOf('task:research:research-index-search')).toBeGreaterThan(ids.indexOf('task:research:reddit-mining'));
    expect(ids.indexOf('task:research:research-index-search')).toBeLessThan(ids.indexOf('task:refinement:edit-pass'));
  });

  it('routes legacy taskId-less messages onto the running row of that phase', () => {
    let state = seeded();
    state = reduceBridgeEvent(
      state,
      event({type: 'task_progress', phase: 'refinement', taskId: 'refinement:edit-pass', data: {status: 'running'}}),
    );
    state = reduceBridgeEvent(
      state,
      event({type: 'task_progress', phase: 'refinement', message: 'Editing sections batch 2/4', data: {advance: 0}}),
    );
    expect(state.progressRows.find(row => row.id === 'task:refinement:edit-pass')?.detail).toBe('Editing sections batch 2/4');
    expect(state.progressRows).toHaveLength(5);
  });

  it('routes unscoped taskIds (queued-comments) as legacy messages, never orphan rows', () => {
    let state = seeded();
    state = reduceBridgeEvent(
      state,
      event({type: 'task_progress', phase: 'refinement', taskId: 'refinement:review-gate', data: {status: 'running'}}),
    );
    state = reduceBridgeEvent(
      state,
      event({type: 'task_progress', phase: 'review', taskId: 'queued-comments', message: 'Queued comment', data: {commentCount: 1}}),
    );
    expect(state.progressRows).toHaveLength(5);
    expect(state.progressRows.find(row => row.id === 'task:refinement:review-gate')?.detail).toBe('Queued comment');
  });

  it('maps review gate lifecycle events onto the seeded Review Gate row', () => {
    let state = seeded();
    state = reduceBridgeEvent(
      state,
      event({type: 'review_gate_opened', phase: 'review', taskId: 'gate-open', message: 'Pre-polish review gate opened'}),
    );
    expect(state.progressRows.find(row => row.id === 'task:refinement:review-gate')?.status).toBe('running');
    expect(state.step).toBe('review');

    state = reduceBridgeEvent(
      state,
      event({type: 'review_gate_closed', phase: 'review', taskId: 'gate-closed', message: 'Review decision: approve'}),
    );
    expect(state.progressRows.find(row => row.id === 'task:refinement:review-gate')?.status).toBe('complete');
  });

  it('settles rows at generation_complete: running→complete, pending→skipped', () => {
    let state = seeded();
    state = reduceBridgeEvent(
      state,
      event({type: 'task_progress', phase: 'research', taskId: 'research:serp-analysis', data: {status: 'running'}}),
    );
    state = reduceBridgeEvent(state, event({type: 'generation_complete', phase: 'preview', data: {}}));
    const byId = new Map(state.progressRows.map(row => [row.id, row.status]));
    expect(byId.get('task:research:serp-analysis')).toBe('complete');
    expect(byId.get('task:research:reddit-mining')).toBe('skipped');
    expect(byId.get('task:refinement:review-gate')).toBe('skipped');
  });

  it('keeps legacy row behaviour when no plan was seeded (version-skewed bridge)', () => {
    let state = createInitialState();
    state = reduceBridgeEvent(state, event({type: 'task_progress', phase: 'generation', message: 'Generating…'}));
    expect(state.progressRows).toHaveLength(1);
    expect(state.progressRows[0]?.id).toBe('phase:generation');
  });
});

describe('todo line building and windowing', () => {
  const rows: ProgressRow[] = PLAN.flatMap(plan =>
    plan.tasks.map(task => ({id: `task:${task.id}`, label: task.label, phase: plan.phase, status: 'pending' as const})),
  );

  it('emits phase headers between groups', () => {
    const lines = buildTodoLines(rows, PLAN.map(({phase, label}) => ({phase, label})));
    expect(lines.map(line => (line.kind === 'phase' ? `# ${line.label}` : line.kind))).toEqual([
      '# Loading Context',
      'task',
      '# Research',
      'task',
      'task',
      '# Refinement',
      'task',
      'task',
    ]);
  });

  it('centers the window on the active row with hidden counts', () => {
    const manyRows: ProgressRow[] = Array.from({length: 20}, (_, index) => ({
      id: `task:draft:part-${index}`,
      label: `Part ${index}`,
      phase: 'draft',
      status: index === 10 ? 'running' : index < 10 ? 'complete' : 'pending',
    }));
    const lines = buildTodoLines(manyRows, [{phase: 'draft', label: 'Draft'}]);
    const active = activeLineIndex(lines);
    const {lines: visible, hiddenAbove, hiddenBelow} = windowTodoLines(lines, 9, active);
    expect(visible.length).toBe(7);
    expect(hiddenAbove).toBeGreaterThan(0);
    expect(hiddenBelow).toBeGreaterThan(0);
    expect(visible.some(line => line.kind === 'task' && line.row.status === 'running')).toBe(true);
  });

  it('renders proportional bars', () => {
    expect(renderBar(5, 10)).toBe('[████░░░░]');
    expect(renderBar(10, 10)).toBe('[████████]');
    expect(renderBar(0, 10)).toBe('[░░░░░░░░]');
  });
});

describe('TodoList rendering', () => {
  it('renders glyphs for every status and the phase headers', () => {
    const rows: ProgressRow[] = [
      {id: 'task:research:serp-analysis', label: 'SERP Analysis', phase: 'research', status: 'complete', detail: '12 questions', elapsed: 3.2},
      {id: 'task:research:reddit-mining', label: 'Reddit Mining', phase: 'research', status: 'running', progressCurrent: 5, progressTotal: 10, query: 'best crm'},
      {id: 'task:research:quora-extraction', label: 'Quora Extraction', phase: 'research', status: 'skipped', detail: 'Disabled'},
      {id: 'task:refinement:edit-pass', label: 'Edit Pass', phase: 'refinement', status: 'error', detail: 'boom'},
      {id: 'task:refinement:polish-pass', label: 'Polish Pass', phase: 'refinement', status: 'pending'},
    ];
    const {lastFrame} = render(
      <ThemeProvider>
        <TodoList rows={rows} planPhases={[{phase: 'research', label: 'Research'}, {phase: 'refinement', label: 'Refinement'}]} tick={0} />
      </ThemeProvider>,
    );
    const frame = lastFrame() ?? '';
    expect(frame).toContain('Research');
    expect(frame).toContain('Refinement');
    expect(frame).toContain('●');
    expect(frame).toContain('⠋'); // running spinner frame at tick 0
    expect(frame).toContain('⊘');
    expect(frame).toContain('✗');
    expect(frame).toContain('○');
    expect(frame).toContain('[████░░░░] 5/10');
    expect(frame).toContain('"best crm"');
    expect(frame).toContain('3.2s');
  });

  it('shows a waiting line before any rows exist', () => {
    const {lastFrame} = render(
      <ThemeProvider>
        <TodoList rows={[]} planPhases={[]} />
      </ThemeProvider>,
    );
    expect(lastFrame()).toContain('Waiting for the run plan…');
  });
});
