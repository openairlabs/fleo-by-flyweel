import React, {useEffect, useRef, useState} from 'react';
import {Box, Text, useInput} from 'ink';
import {Spinner} from '@inkjs/ui';
import path from 'node:path';

import {installationRoot} from '../bridge.js';
import {ensurePythonRuntime} from '../lib/pythonBootstrap.js';
import {useLayout} from '../lib/useTerminalSize.js';
import {
  PIPELINE_STEP_MODELS,
  formatModelOptionLabel,
  friendlyModelName,
  type ModelTier,
  type PipelineStepModelSpec,
} from '../modelCatalog.js';
import {maskSetupValue} from '../state.js';
import {
  runDetectSiteTargetAction,
  runSaveBrandProfileAction,
  runSaveSetupDirect,
  runScanBrandUrlAction,
  runTestKeysAction,
} from '../setupActions.js';
import {KeyValue, OptionList, SetupFlow, StatusMessage} from './termcn.js';
import {TextInput} from './textInput.js';
import {useTheme} from './ui/theme-provider.js';
import type {AppAction, AppState} from '../types.js';

type WizardStep = 'hub' | 'runtime' | 'keys' | 'brand' | 'target' | 'ready';

type ModelsPickerState =
  | null
  | {mode: 'list'; origin: 'simple' | 'hub'}
  | {mode: 'step'; role: PipelineStepModelSpec['role']; origin: 'simple' | 'hub' | 'agents'};

const WIZARD_STEPS: Array<{id: Exclude<WizardStep, 'hub'>; label: string}> = [
  {id: 'runtime', label: '1 Runtime'},
  {id: 'keys', label: '2 API Keys'},
  {id: 'brand', label: '3 Your Brand'},
  {id: 'target', label: '4 Website Stack'},
  {id: 'ready', label: '5 Ready'},
];

const STACK_OPTIONS = [
  {value: 'nextjs', label: 'Next.js — App Router MDX / Contentlayer / Fumadocs'},
  {value: 'astro', label: 'Astro — Content Collections (.mdx / .md)'},
  {value: 'webflow', label: 'Webflow — Rich Text HTML + CMS CSV + Head Schema'},
  {value: 'framer', label: 'Framer — CMS Markdown + CSV + Head Schema'},
  {value: 'nuxt', label: 'Nuxt — @nuxt/content (.md)'},
  {value: 'sveltekit', label: 'SvelteKit — mdsvex (.md / .svx)'},
  {value: 'remix', label: 'Remix / React Router v7'},
  {value: 'markdown', label: 'Not sure / Other — Universal Markdown + Agent Auto-Detect'},
];

const INTERVIEW_QUESTIONS: Array<{key: string; title: string; placeholder: string}> = [
  {key: 'company_name', title: '1/5 · What is your company or product name?', placeholder: 'e.g. Linear, Resend, Acme AI'},
  {key: 'site_url', title: '2/5 · What is your website URL? (Enter to skip if pre-launch)', placeholder: 'e.g. https://acme.ai'},
  {key: 'what_you_sell', title: '3/5 · In 1-2 plain sentences, what do you sell & how is it priced?', placeholder: 'e.g. Open-source feature flags for engineering teams; free tier + $49/mo Pro'},
  {key: 'icp_and_pain', title: '4/5 · Who buys it, and what is their #1 frustration before finding you?', placeholder: 'e.g. Startup CTOs tired of slow deploys and brittle homegrown config flags'},
  {key: 'competitors', title: '5/5 · Name 2-3 alternatives/competitors (comma-separated):', placeholder: 'e.g. LaunchDarkly, Statsig, PostHog'},
];

interface BrandDraft {
  company_name: string;
  site_url: string;
  category: string;
  one_liner: string;
  icp_reader: string;
  tone: string;
  direct_competitors: string[];
  compliance_rules: string[];
  [key: string]: unknown;
}

const StepRail = ({current}: {current: WizardStep}) => {
  const theme = useTheme();
  const {columns} = useLayout();
  const compact = columns < 76;

  if (current === 'hub') {
    return (
      <Box marginBottom={1}>
        <Text bold color={theme.colors.primary} wrap="truncate-end">
          ✦ Fleo Setup & Brand Hub
        </Text>
        {!compact ? (
          <Text color={theme.colors.mutedForeground} wrap="truncate-end">
            {'  ·  Configure keys, models, brand context, and target website'}
          </Text>
        ) : null}
      </Box>
    );
  }

  const activeIdx = WIZARD_STEPS.findIndex(s => s.id === current);
  if (compact) {
    const activeLabel = WIZARD_STEPS[activeIdx]?.label ?? current;
    return (
      <Box marginBottom={1}>
        <Text color={theme.colors.mutedForeground}>{`Step ${activeIdx + 1}/${WIZARD_STEPS.length} · `}</Text>
        <Text bold color={theme.colors.primary} wrap="truncate-end">
          {`▸ ${activeLabel}`}
        </Text>
      </Box>
    );
  }

  return (
    <Box marginBottom={1}>
      {WIZARD_STEPS.map((s, idx) => {
        const active = s.id === current;
        const done = activeIdx > idx;
        return (
          <React.Fragment key={s.id}>
            {idx > 0 ? <Text color={theme.colors.mutedForeground}> › </Text> : null}
            <Text
              bold={active}
              color={active ? theme.colors.primary : done ? theme.colors.success : theme.colors.mutedForeground}
            >
              {done ? `✓ ${s.label}` : active ? `▸ ${s.label}` : s.label}
            </Text>
          </React.Fragment>
        );
      })}
    </Box>
  );
};

export const SetupView = ({
  state,
  dispatch,
  noBridge = false,
  setupOnly = false,
  onExit,
}: {
  state: AppState;
  dispatch?: React.Dispatch<AppAction>;
  noBridge?: boolean;
  setupOnly?: boolean;
  onExit?: () => void;
}) => {
  const theme = useTheme();
  const {columns, bodyRows} = useLayout();
  const compactCols = columns < 88;

  // Compatibility fallback when rendered in static snapshot / noBridge tests
  if (!dispatch || noBridge) {
    return (
      <>
        {state.setupStatus ? (
          <>
            <SetupFlow checks={state.setupStatus.checks} />
            <StatusMessage tone={state.setupStatus.canGenerate ? 'success' : 'error'}>
              {state.setupStatus.canGenerate
                ? 'Required setup checks passed.'
                : `Missing required setup: ${state.setupStatus.requiredMissing.join(', ')}`}
            </StatusMessage>
          </>
        ) : (
          <StatusMessage tone="warning">Setup status has not been loaded yet.</StatusMessage>
        )}
        {Object.keys(state.setupDraft).length > 0 ? (
          <>
            <Text bold>Staged changes</Text>
            <KeyValue items={Object.entries(state.setupDraft).map(([k, v]) => [k, maskSetupValue(k, v)])} />
          </>
        ) : null}
        <StatusMessage tone="info">Press r to re-check or Esc to go back.</StatusMessage>
      </>
    );
  }

  const status = state.setupStatus;
  const initialWizardStep = (): WizardStep => {
    if (!status) return 'runtime';
    if (status.canGenerate && status.onboardingComplete) return 'hub';
    const venvOk = status.checks.find(c => c.id === 'venv')?.ok ?? true;
    if (!venvOk) return 'runtime';
    if (!status.canGenerate) return 'keys';
    if (!status.brandConfigured) return 'brand';
    return 'target';
  };

  const [wizStep, setWizStep] = useState<WizardStep>(initialWizardStep);
  const [didInitStep, setDidInitStep] = useState(Boolean(status));

  useEffect(() => {
    if (!didInitStep && status) {
      setDidInitStep(true);
      setWizStep(initialWizardStep());
    }
  }, [didInitStep, status]);

  // Shared async status / error banner
  const [busyMessage, setBusyMessage] = useState<string | null>(null);
  const [feedback, setFeedback] = useState<{tone: 'info' | 'success' | 'warning' | 'error'; text: string} | null>(null);

  // Step 2: Keys & Per-Step Models state
  const [keyMode, setKeyMode] = useState<'simple' | 'agents'>('simple');
  const [keyValues, setKeyValues] = useState<Record<string, string>>({});
  const configuredTier: ModelTier = status?.agentRoles?.find(r => r.role === 'DRAFT')?.model === 'anthropic/claude-opus-5.5' &&
    status?.agentRoles?.find(r => r.role === 'POLISH')?.model === 'anthropic/claude-opus-5.5'
    ? 'max'
    : 'balanced';
  const displayedTier: ModelTier = keyValues.MODEL_TIER === 'max' ? 'max' : keyValues.MODEL_TIER === 'balanced' ? 'balanced' : configuredTier;
  const [editingEnvKey, setEditingEnvKey] = useState<{env: string; label: string; placeholder: string; secret: boolean} | null>(null);
  const [modelsPicker, setModelsPicker] = useState<ModelsPickerState>(null);

  // Step 3: Brand state
  const [brandSubView, setBrandSubView] = useState<'menu' | 'url-input' | 'review-draft' | 'edit-draft-field' | 'interview' | 'prompt-info'>('menu');
  const [brandDraft, setBrandDraft] = useState<BrandDraft | null>(null);
  const [editingBrandField, setEditingBrandField] = useState<{key: keyof BrandDraft; label: string} | null>(null);
  const [interviewIdx, setInterviewIdx] = useState(0);
  const [interviewAnswers, setInterviewAnswers] = useState<Record<string, string>>({});
  const [detectedStackHint, setDetectedStackHint] = useState<string>('nextjs');

  // Step 4: Website Target state
  const [targetSubView, setTargetSubView] = useState<'menu' | 'pick-stack' | 'repo-path' | 'content-dir'>('menu');
  const [pendingRepoPath, setPendingRepoPath] = useState<string>(process.cwd());

  const resolveStepModel = (spec: PipelineStepModelSpec): {slug: string; isDefault: boolean} => {
    const tierDefault = spec.defaults[displayedTier];
    if (Object.prototype.hasOwnProperty.call(keyValues, spec.modelEnv)) {
      const staged = keyValues[spec.modelEnv]?.trim() ?? '';
      const slug = staged || tierDefault;
      return {slug, isDefault: slug === tierDefault};
    }
    const statusRole = status?.agentRoles?.find(r => r.role === spec.role);
    // When user toggled MODEL_TIER in UI without explicit role override, reflect the new tier default
    const prevTierDefault = spec.defaults[configuredTier];
    const fromStatus = statusRole?.model ?? tierDefault;
    const slug = keyValues.MODEL_TIER && fromStatus === prevTierDefault ? tierDefault : fromStatus;
    return {slug, isDefault: slug === tierDefault};
  };

  const customStepCount = PIPELINE_STEP_MODELS.filter(spec => !resolveStepModel(spec).isDefault).length;

  // Step 1: Auto-check / bootstrap Python runtime when landing on `runtime`
  const runtimeBusyRef = useRef(false);
  useEffect(() => {
    if (wizStep !== 'runtime') return;
    const controller = new AbortController();
    runtimeBusyRef.current = true;
    setBusyMessage('Checking Python runtime & packages…');
    void ensurePythonRuntime(msg => {
      if (!controller.signal.aborted) setBusyMessage(msg);
    }, controller.signal).then(res => {
      if (controller.signal.aborted) return;
      runtimeBusyRef.current = false;
      setBusyMessage(null);
      if (res.ok) {
        dispatch({type: 'request_setup'});
        setFeedback({tone: 'success', text: res.bootstrapped ? 'Python engine installed automatically.' : 'Python engine ready.'});
        setWizStep(status?.canGenerate ? (status.brandConfigured ? 'target' : 'brand') : 'keys');
      } else {
        setFeedback({tone: 'error', text: res.detail});
      }
    });
    return () => {
      if (runtimeBusyRef.current) {
        runtimeBusyRef.current = false;
        setBusyMessage(null);
      }
      controller.abort();
    };
  }, [wizStep]);

  const leaveSetup = () => {
    if (setupOnly && onExit) {
      onExit();
      return;
    }
    dispatch({type: 'set_step', step: 'config'});
  };

  const isTyping =
    Boolean(editingEnvKey) ||
    brandSubView === 'url-input' ||
    brandSubView === 'edit-draft-field' ||
    brandSubView === 'interview' ||
    targetSubView === 'repo-path' ||
    targetSubView === 'content-dir';

  useInput((input, key) => {
    if (busyMessage) return;
    if (key.tab && wizStep === 'keys' && !editingEnvKey && !modelsPicker) {
      setKeyMode(m => (m === 'simple' ? 'agents' : 'simple'));
      return;
    }
    if (!isTyping && input === 'r') {
      dispatch({type: 'request_setup'});
      setFeedback({tone: 'info', text: 'Refreshed setup checks.'});
      return;
    }
    if (key.escape) {
      if (editingEnvKey) {
        setEditingEnvKey(null);
        return;
      }
      if (modelsPicker) {
        if (modelsPicker.mode === 'step') {
          if (modelsPicker.origin === 'agents') {
            setModelsPicker(null);
          } else {
            setModelsPicker({mode: 'list', origin: modelsPicker.origin});
          }
          return;
        }
        const origin = modelsPicker.origin;
        setModelsPicker(null);
        if (origin === 'hub') setWizStep('hub');
        return;
      }
      if (brandSubView === 'edit-draft-field') {
        setBrandSubView('review-draft');
        setEditingBrandField(null);
        return;
      }
      if (brandSubView !== 'menu') {
        setBrandSubView('menu');
        return;
      }
      if (targetSubView === 'content-dir') {
        setTargetSubView('repo-path');
        return;
      }
      if (targetSubView !== 'menu') {
        setTargetSubView('menu');
        return;
      }
      if (wizStep === 'ready') return setWizStep('target');
      if (wizStep === 'target') return setWizStep('brand');
      if (wizStep === 'brand') return setWizStep('keys');
      leaveSetup();
    }
  });

  const listViewport = Math.max(3, bodyRows - (feedback ? 10 : 8));

  const collectSetupPayload = (): Record<string, string> => {
    const out: Record<string, string> = {};
    for (const [k, v] of Object.entries(keyValues)) {
      const trimmed = v.trim();
      // Empty string is meaningful ONLY for clearing model overrides back to tier defaults
      if (trimmed.length > 0 || k.endsWith('_MODEL')) {
        out[k] = trimmed;
      }
    }
    return out;
  };

  const handleSaveModelsOnly = async (returnToHub: boolean) => {
    const staged = collectSetupPayload();
    if (Object.keys(staged).length > 0) {
      setBusyMessage('Saving model selections to .env (0600)…');
      setFeedback(null);
      const saveRes = await runSaveSetupDirect(staged);
      setBusyMessage(null);
      dispatch({type: 'bridge_events', events: saveRes.events});
      if (saveRes.diagnostic) {
        setFeedback({tone: 'error', text: saveRes.diagnostic});
        return;
      }
      setKeyValues({});
      setFeedback({tone: 'success', text: 'Saved per-step model selections.'});
    }
    setModelsPicker(null);
    if (returnToHub) setWizStep('hub');
  };

  const handleVerifyAndSaveKeys = async () => {
    const staged = collectSetupPayload();
    const hasNewKey = Object.entries(staged).some(([k, v]) => k.endsWith('_API_KEY') && v.length > 0);

    if (!hasNewKey && !status?.canGenerate) {
      setFeedback({tone: 'warning', text: 'Paste your OPENROUTER_API_KEY first (press Enter on row 1).'});
      return;
    }

    setBusyMessage('Saving .env (0600) & testing live API connection…');
    setFeedback(null);
    if (Object.keys(staged).length > 0) {
      const saveRes = await runSaveSetupDirect(staged);
      dispatch({type: 'bridge_events', events: saveRes.events});
      if (saveRes.diagnostic) {
        setBusyMessage(null);
        setFeedback({tone: 'error', text: saveRes.diagnostic});
        return;
      }
    }

    const testPayload = Object.fromEntries(Object.entries(staged).filter(([, v]) => v.length > 0));
    const testRes = await runTestKeysAction(testPayload);
    setBusyMessage(null);
    const artifact = testRes.events.find(e => e.type === 'artifact_ready');
    const data = (artifact?.data ?? {}) as {allCoreReady?: boolean; roles?: Record<string, {ok?: boolean; detail?: string}>};
    if (data.allCoreReady) {
      const draftDetail = data.roles?.DRAFT?.detail || 'API verified';
      setKeyValues({});
      setFeedback({tone: 'success', text: `${draftDetail} — ready for all agent roles.`});
      setWizStep('brand');
    } else {
      const firstFail = Object.values(data.roles ?? {}).find(r => !r.ok)?.detail || testRes.diagnostic || 'Key verification failed — double-check your API key.';
      setFeedback({tone: 'warning', text: `${firstFail} (Saved to .env — fix or press Continue anyway).`});
    }
  };

  const handleScanUrl = async (rawUrl: string) => {
    const trimmed = rawUrl.trim();
    if (!trimmed) return;
    setBusyMessage(`Scanning ${trimmed} (llms.txt, sitemap & key pages)…`);
    setFeedback(null);
    const res = await runScanBrandUrlAction(trimmed);
    setBusyMessage(null);
    if (res.diagnostic) {
      setFeedback({tone: 'error', text: res.diagnostic});
      setBrandSubView('menu');
      return;
    }
    const art = res.events.find(e => e.type === 'artifact_ready');
    const data = (art?.data ?? {}) as {profile?: BrandDraft; pagesCrawled?: number; detectedFramework?: string};
    if (data.detectedFramework) setDetectedStackHint(data.detectedFramework);
    if (data.profile) {
      setBrandDraft(data.profile);
      setFeedback({
        tone: 'success',
        text: `Scanned ${data.pagesCrawled ?? 1} page(s) · drafted ${data.profile.company_name || 'Brand'} profile.`,
      });
      setBrandSubView('review-draft');
    }
  };

  const handleSaveBrandPayload = async (payload: Record<string, unknown>, nextMessage: string) => {
    setBusyMessage('Saving brand profile & syncing .agents/product-marketing.md…');
    const res = await runSaveBrandProfileAction(payload);
    setBusyMessage(null);
    dispatch({type: 'bridge_events', events: res.events});
    if (res.diagnostic) {
      setFeedback({tone: 'error', text: res.diagnostic});
      return;
    }
    if (payload.skipBrand) {
      dispatch({type: 'update_config', patch: {flyweelMode: 'none'}});
    } else {
      dispatch({type: 'update_config', patch: {flyweelMode: 'full'}});
    }
    setFeedback({tone: 'success', text: nextMessage});
    setBrandSubView('menu');
    setWizStep('target');
  };

  const handleSaveSiteTarget = async (payload: {mode: 'handoff' | 'repo'; framework?: string; repoPath?: string; contentDir?: string}) => {
    setBusyMessage(payload.mode === 'repo' ? 'Inspecting repository framework & existing blog posts…' : 'Saving website stack preference…');
    const res = await runDetectSiteTargetAction(payload);
    setBusyMessage(null);
    dispatch({type: 'bridge_events', events: res.events});
    if (res.diagnostic) {
      setFeedback({tone: 'error', text: res.diagnostic});
      return;
    }
    dispatch({type: 'update_config', patch: {saveDefault: payload.mode === 'repo' ? 'publish' : 'local'}});
    setTargetSubView('menu');
    setFeedback({tone: 'success', text: 'Website target configured.'});
    setWizStep('ready');
  };

  const renderModelsPicker = () => {
    if (!modelsPicker) return null;

    if (modelsPicker.mode === 'step') {
      const spec = PIPELINE_STEP_MODELS.find(s => s.role === modelsPicker.role);
      if (!spec) return null;
      const current = resolveStepModel(spec);
      const tierDefault = spec.defaults[displayedTier];
      const presetOptions = spec.options.map(opt => ({
        value: `slug:${opt.slug}`,
        label: formatModelOptionLabel(opt, tierDefault, current.slug, compactCols),
      }));
      const isCustomUnlisted = !spec.options.some(o => o.slug === current.slug);
      const allOptions = [
        ...(isCustomUnlisted
          ? [{value: `slug:${current.slug}`, label: `● ${current.slug} [Active Custom Slug]`}]
          : []),
        ...presetOptions,
        {value: '__custom_slug', label: '✎ Enter custom model slug (any OpenRouter / provider ID)…'},
        {value: '__back', label: '← Back'},
      ];
      const activeIdx = Math.max(
        0,
        allOptions.findIndex(o => o.value === `slug:${current.slug}`),
      );

      return (
        <Box flexDirection="column">
          <Text bold color={theme.colors.primary} wrap="truncate-end">
            {`Step ${spec.stepNumber} · ${spec.title}`}
          </Text>
          <Text color={theme.colors.mutedForeground} wrap="truncate-end">
            {spec.subtitle}
          </Text>
          <Box marginTop={1}>
            <OptionList
              key={`${spec.role}-${displayedTier}-${current.slug}`}
              viewport={listViewport}
              initialIndex={activeIdx}
              options={allOptions}
              onSelect={val => {
                if (val === '__back') {
                  if (modelsPicker.origin === 'agents') setModelsPicker(null);
                  else setModelsPicker({mode: 'list', origin: modelsPicker.origin});
                  return;
                }
                if (val === '__custom_slug') {
                  setEditingEnvKey({
                    env: spec.modelEnv,
                    label: `Custom model slug for ${spec.title} (${spec.modelEnv}):`,
                    placeholder: `e.g. ${tierDefault}`,
                    secret: false,
                  });
                  return;
                }
                if (val.startsWith('slug:')) {
                  const chosen = val.slice(5);
                  // Store empty string when matching tier default so .env removes stale overrides cleanly
                  setKeyValues(prev => ({
                    ...prev,
                    [spec.modelEnv]: chosen === tierDefault ? '' : chosen,
                  }));
                  if (modelsPicker.origin === 'agents') setModelsPicker(null);
                  else setModelsPicker({mode: 'list', origin: modelsPicker.origin});
                }
              }}
            />
          </Box>
          <Box marginTop={1}>
            <Text color={theme.colors.mutedForeground} wrap="truncate-end">
              ↑↓ move · Enter select model · Esc back
            </Text>
          </Box>
        </Box>
      );
    }

    // modelsPicker.mode === 'list'
    const stepColWidth = compactCols ? 16 : 31;
    const stepOptions = PIPELINE_STEP_MODELS.map(spec => {
      const {slug, isDefault} = resolveStepModel(spec);
      const stepLabel = `${spec.stepNumber} · ${compactCols ? spec.shortTitle : spec.title}`.padEnd(stepColWidth);
      const modelLabel = friendlyModelName(slug);
      const badge = isDefault ? '[Default · Recommended]' : '[Custom]';
      return {
        value: `role:${spec.role}`,
        label: `${stepLabel} ${modelLabel} ${badge}`,
      };
    });

    const fromHub = modelsPicker.origin === 'hub';
    return (
      <Box flexDirection="column">
        <Text bold color={theme.colors.primary} wrap="truncate-end">
          ✦ Choose AI Model for Each Pipeline Step
        </Text>
        <Text color={theme.colors.mutedForeground} wrap="truncate-end">
          Recommended defaults are pre-selected. Press Enter on any step to switch models:
        </Text>
        <Box marginTop={1}>
          <OptionList
            viewport={listViewport}
            options={[
              ...stepOptions,
              {value: '__reset_defaults', label: '↺ Reset All Steps to Recommended Defaults'},
              {
                value: '__done_models',
                label: fromHub
                  ? '✓ Save Model Choices & Return to Setup Hub'
                  : status?.canGenerate && Object.keys(collectSetupPayload()).length > 0
                    ? '✓ Save Model Choices & Return to API Keys'
                    : '← Done — Return to API Keys',
              },
            ]}
            onSelect={val => {
              if (val === '__reset_defaults') {
                const cleared: Record<string, string> = {};
                for (const s of PIPELINE_STEP_MODELS) cleared[s.modelEnv] = '';
                setKeyValues(prev => ({...prev, ...cleared}));
                setFeedback({tone: 'info', text: 'Reset all pipeline steps to Recommended Defaults.'});
                return;
              }
              if (val === '__done_models') {
                if (fromHub || status?.canGenerate) {
                  void handleSaveModelsOnly(fromHub);
                } else {
                  setModelsPicker(null);
                }
                return;
              }
              if (val.startsWith('role:')) {
                const role = val.slice(5) as PipelineStepModelSpec['role'];
                setModelsPicker({mode: 'step', role, origin: modelsPicker.origin});
              }
            }}
          />
        </Box>
        <Box marginTop={1}>
          <Text color={theme.colors.mutedForeground} wrap="truncate-end">
            ↑↓ move · Enter pick model for step · Esc back
          </Text>
        </Box>
      </Box>
    );
  };

  return (
    <Box flexDirection="column" width="100%">
      <StepRail current={wizStep} />

      {feedback ? (
        <Box marginBottom={1}>
          <StatusMessage tone={feedback.tone}>{feedback.text}</StatusMessage>
        </Box>
      ) : null}

      {busyMessage ? (
        <Box marginY={1}>
          <Spinner label={busyMessage} />
        </Box>
      ) : null}

      {/*HUB VIEW (when already configured)*/}
      {!busyMessage && wizStep === 'hub' ? (
        <Box flexDirection="column">
          <KeyValue
            items={[
              ['AI Credentials', status?.canGenerate ? '[OK] Ready' : '[MISS] Needs API key'],
              [
                'Pipeline Models',
                customStepCount > 0
                  ? `${customStepCount} custom step(s) · ${displayedTier} tier`
                  : `Recommended Defaults (${displayedTier} tier)`,
              ],
              ['Brand Profile', status?.brandConfigured ? `[OK] ${status.brandName} (${status.brandCategory || 'Configured'})` : '[OPT] Unconfigured (Educational Mode)'],
              ['Website Target', status?.siteTarget?.frameworkLabel ?? 'Agent Handoff (.md)'],
            ]}
          />
          <Box marginTop={1}>
            <OptionList
              viewport={listViewport}
              options={[
                {value: 'go', label: '▶ Start writing articles'},
                {value: 'keys', label: '1 · Edit API Keys'},
                {value: 'models', label: '2 · Choose Models per Pipeline Step (Default / Recommended)'},
                {value: 'brand', label: '3 · Scan Website or Edit Brand Context'},
                {value: 'target', label: '4 · Change Website Stack / Direct Repo Publish (Experimental)'},
                {value: 'test', label: '5 · Run Live API Connectivity Test'},
              ]}
              onSelect={val => {
                if (val === 'go') leaveSetup();
                else if (val === 'keys') setWizStep('keys');
                else if (val === 'models') {
                  setWizStep('keys');
                  setModelsPicker({mode: 'list', origin: 'hub'});
                } else if (val === 'brand') setWizStep('brand');
                else if (val === 'target') setWizStep('target');
                else if (val === 'test') void handleVerifyAndSaveKeys();
              }}
            />
          </Box>
          <Box marginTop={1}>
            <Text color={theme.colors.mutedForeground} wrap="truncate-end">
              ↑↓ move · Enter select · r refresh · Esc back
            </Text>
          </Box>
        </Box>
      ) : null}

      {/*STEP 1: RUNTIME*/}
      {!busyMessage && wizStep === 'runtime' ? (
        <Box flexDirection="column">
          <Text wrap="truncate-end">Checking local Python engine environment…</Text>
          <Box marginTop={1}>
            <OptionList
              options={[
                {value: 'retry', label: 'Retry automatic Python environment install'},
                {value: 'skip', label: 'Continue to API Keys →'},
              ]}
              onSelect={v => {
                if (v === 'skip') setWizStep('keys');
                else {
                  setWizStep('hub');
                  setTimeout(() => setWizStep('runtime'), 10);
                }
              }}
            />
          </Box>
        </Box>
      ) : null}

      {/*STEP 2: API KEYS & PER-STEP MODELS*/}
      {!busyMessage && wizStep === 'keys' ? (
        <Box flexDirection="column">
          {!modelsPicker ? (
            <Box marginBottom={1}>
              <Text bold color={keyMode === 'simple' ? theme.colors.primary : theme.colors.mutedForeground} wrap="truncate-end">
                {keyMode === 'simple' ? '▸ Quickstart (1 Key) ◂' : '  Quickstart (1 Key)  '}
              </Text>
              <Text color={theme.colors.mutedForeground}>{'  '}</Text>
              <Text bold color={keyMode === 'agents' ? theme.colors.primary : theme.colors.mutedForeground} wrap="truncate-end">
                {keyMode === 'agents' ? '▸ Separate Per-Agent Keys ◂' : '  Separate Per-Agent Keys  '}
              </Text>
              {!compactCols ? <Text color={theme.colors.mutedForeground}>{'  (Tab to switch)'}</Text> : null}
            </Box>
          ) : null}

          {editingEnvKey ? (
            <Box flexDirection="column">
              <Text bold wrap="truncate-end">
                {editingEnvKey.label}
              </Text>
              <Text color={theme.colors.mutedForeground} wrap="truncate-end">
                Saved locally to .env (0600) — ensure .env is in .gitignore.
              </Text>
              <Box marginTop={1}>
                <TextInput
                  key={editingEnvKey.env}
                  defaultValue={keyValues[editingEnvKey.env] ?? ''}
                  placeholder={editingEnvKey.placeholder}
                  mask={editingEnvKey.secret}
                  onSubmit={val => {
                    setKeyValues(prev => ({...prev, [editingEnvKey.env]: val.trim()}));
                    setEditingEnvKey(null);
                    if (modelsPicker?.mode === 'step') {
                      setModelsPicker(
                        modelsPicker.origin === 'agents'
                          ? null
                          : {mode: 'list', origin: modelsPicker.origin},
                      );
                    }
                  }}
                />
              </Box>
              <Box marginTop={1}>
                <Text color={theme.colors.mutedForeground} wrap="truncate-end">
                  Paste value & press Enter · Esc cancel
                </Text>
              </Box>
            </Box>
          ) : modelsPicker ? (
            renderModelsPicker()
          ) : keyMode === 'simple' ? (
            <Box flexDirection="column">
              <Text color={theme.colors.mutedForeground} wrap="truncate-end">
                One OpenRouter key powers Research, Writing, Polish, SEO, and Embeddings (~$0.23/post).
              </Text>
              <Box marginTop={1}>
                <OptionList
                  viewport={listViewport}
                  options={[
                    {
                      value: 'or_key',
                      label: `OpenRouter API Key (Required)    ${
                        keyValues.OPENROUTER_API_KEY
                          ? `[staged: ${maskSetupValue('OPENROUTER_API_KEY', keyValues.OPENROUTER_API_KEY)}]`
                          : status?.canGenerate
                            ? '[OK in .env]'
                            : '[paste sk-or-... from openrouter.ai/keys]'
                      }`,
                    },
                    {
                      value: 'pplx_key',
                      label: `Perplexity API Key (Optional)    ${
                        keyValues.PERPLEXITY_API_KEY
                          ? `[staged: ${maskSetupValue('PERPLEXITY_API_KEY', keyValues.PERPLEXITY_API_KEY)}]`
                          : status?.checks.find(c => c.id === 'perplexity')?.ok
                            ? '[OK in .env]'
                            : '[optional native Sonar Reddit/SERP citations]'
                      }`,
                    },
                    {
                      value: 'tier',
                      label: `Quality / Cost Preset            ${displayedTier === 'max' ? 'max (Claude Opus 5.5 · ~$0.76/post)' : 'balanced (GPT-6 Sol + Sonnet 5.5 · ~$0.23/post)'}`,
                    },
                    {
                      value: 'pick_models',
                      label: `Choose Models per Step           ${
                        customStepCount > 0
                          ? `${customStepCount} custom model(s) · Enter to edit →`
                          : 'Recommended Defaults (Enter to customize →)'
                      }`,
                    },
                    {value: 'verify_next', label: '✓ Verify & Save Keys → Continue to Brand Setup'},
                    ...(status?.canGenerate ? [{value: 'skip_next', label: '→ Keys already configured — Skip to Brand Setup'}] : []),
                  ]}
                  onSelect={val => {
                    if (val === 'or_key') {
                      setEditingEnvKey({
                        env: 'OPENROUTER_API_KEY',
                        label: 'Paste OPENROUTER_API_KEY (https://openrouter.ai/keys):',
                        placeholder: 'sk-or-v1-...',
                        secret: true,
                      });
                    } else if (val === 'pplx_key') {
                      setEditingEnvKey({
                        env: 'PERPLEXITY_API_KEY',
                        label: 'Paste PERPLEXITY_API_KEY (optional — https://perplexity.ai/settings/api):',
                        placeholder: 'pplx-...',
                        secret: true,
                      });
                    } else if (val === 'tier') {
                      setKeyValues(prev => ({...prev, MODEL_TIER: displayedTier === 'max' ? 'balanced' : 'max'}));
                    } else if (val === 'pick_models') {
                      setModelsPicker({mode: 'list', origin: 'simple'});
                    } else if (val === 'verify_next') {
                      void handleVerifyAndSaveKeys();
                    } else if (val === 'skip_next') {
                      setWizStep('brand');
                    }
                  }}
                />
              </Box>
              <Box marginTop={1}>
                <Text color={theme.colors.mutedForeground} wrap="truncate-end">
                  ↑↓ move · Enter select · Tab per-agent keys · Esc back
                </Text>
              </Box>
            </Box>
          ) : (
            <Box flexDirection="column">
              <Text color={theme.colors.mutedForeground} wrap="truncate-end">
                Assign separate API keys or pick models per agent role (unset keys use OPENROUTER_API_KEY).
              </Text>
              <Box marginTop={1}>
                <OptionList
                  viewport={listViewport}
                  options={[
                    ...(status?.agentRoles ?? [
                      {role: 'RESEARCH', label: 'Research Agent (SERP/Reddit)', keyEnv: 'FLEO_RESEARCH_API_KEY', modelEnv: 'FLEO_RESEARCH_MODEL', configured: false, provider: 'missing', model: 'sonar-reasoning-pro', sourceEnv: ''},
                      {role: 'DRAFT', label: 'Writer / Draft Agent', keyEnv: 'FLEO_DRAFT_API_KEY', modelEnv: 'FLEO_DRAFT_MODEL', configured: false, provider: 'missing', model: 'openai/gpt-6-sol', sourceEnv: ''},
                      {role: 'POLISH', label: 'Polish / Humanizer Agent', keyEnv: 'FLEO_POLISH_API_KEY', modelEnv: 'FLEO_POLISH_MODEL', configured: false, provider: 'missing', model: 'anthropic/claude-sonnet-5.5', sourceEnv: ''},
                      {role: 'SEO', label: 'SEO & Section Editor Agent', keyEnv: 'FLEO_SEO_API_KEY', modelEnv: 'FLEO_SEO_MODEL', configured: false, provider: 'missing', model: 'google/gemini-3.8-flash', sourceEnv: ''},
                      {role: 'LIGHT', label: 'Classifier & GEO Judge Agent', keyEnv: 'FLEO_LIGHT_API_KEY', modelEnv: 'FLEO_LIGHT_MODEL', configured: false, provider: 'missing', model: 'openai/gpt-6-luna', sourceEnv: ''},
                      {role: 'EMBED', label: 'Embeddings Agent', keyEnv: 'FLEO_EMBED_API_KEY', modelEnv: 'FLEO_EMBED_MODEL', configured: false, provider: 'missing', model: 'qwen/qwen3-embedding-8b', sourceEnv: ''},
                    ]).flatMap(r => {
                      const catalogSpec = PIPELINE_STEP_MODELS.find(s => s.role === r.role);
                      const resolved = catalogSpec ? resolveStepModel(catalogSpec) : null;
                      const activeModel = resolved ? resolved.slug : keyValues[r.modelEnv] || r.model;
                      const modelTag = resolved
                        ? resolved.isDefault
                          ? '[Default · Recommended]'
                          : '[Custom]'
                        : '';
                      return [
                        {
                          value: `key|${r.keyEnv}|${r.label}|${r.role}`,
                          label: `${r.role.padEnd(10)} Key   ${
                            keyValues[r.keyEnv]
                              ? '[staged]'
                              : r.configured
                                ? `[OK via ${r.sourceEnv}]`
                                : '[unset]'
                          }  (${r.keyEnv})`,
                        },
                        {
                          value: `model|${r.modelEnv}|${r.label}|${r.role}`,
                          label: `${r.role.padEnd(10)} Model ${friendlyModelName(activeModel)} ${modelTag}`.trimEnd(),
                        },
                      ];
                    }),
                    {value: '__save_agents', label: '✓ Save & Test Agent Keys → Continue to Brand Setup'},
                  ]}
                  onSelect={val => {
                    if (val === '__save_agents') {
                      void handleVerifyAndSaveKeys();
                      return;
                    }
                    const [kind, envName, label, role] = val.split('|');
                    if (kind === 'key' && envName) {
                      setEditingEnvKey({
                        env: envName,
                        label: `Set ${envName} (${label}):`,
                        placeholder: 'sk-or-... / sk-... / AIza... / pplx-...',
                        secret: true,
                      });
                    } else if (kind === 'model' && envName) {
                      const catalogSpec = PIPELINE_STEP_MODELS.find(s => s.role === role);
                      if (catalogSpec) {
                        setModelsPicker({mode: 'step', role: catalogSpec.role, origin: 'agents'});
                      } else {
                        setEditingEnvKey({
                          env: envName,
                          label: `Override ${envName} (${label}):`,
                          placeholder: '1 or 0',
                          secret: false,
                        });
                      }
                    }
                  }}
                />
              </Box>
              <Box marginTop={1}>
                <Text color={theme.colors.mutedForeground} wrap="truncate-end">
                  ↑↓ move · Enter edit key or pick model · Tab simple mode · Esc back
                </Text>
              </Box>
            </Box>
          )}
        </Box>
      ) : null}

      {/*STEP 3: YOUR BRAND CONTEXT*/}
      {!busyMessage && wizStep === 'brand' ? (
        <Box flexDirection="column">
          {brandSubView === 'menu' ? (
            <>
              <Text wrap="truncate-end">Teach Fleo about your company so research, angles, and positioning fit your brand:</Text>
              <Box marginTop={1}>
                <OptionList
                  viewport={listViewport}
                  options={[
                    {value: 'scan', label: '✦ Auto-Scan My Website URL (Recommended — ~15s)'},
                    {value: 'interview', label: '✦ 5-Question Quick Interview (Great if pre-launch or no site yet)'},
                    ...(status?.productMarketingFile
                      ? [{value: 'import_pm', label: `✦ Import Detected Context (${path.basename(status.productMarketingFile)})`}]
                      : []),
                    {value: 'prompt', label: '✦ Get Copy-Paste Prompt for ChatGPT / Claude / Cursor'},
                    {value: 'skip', label: '→ Skip Brand Setup for Now (Write Educational / Unbranded Content)'},
                  ]}
                  onSelect={val => {
                    if (val === 'scan') setBrandSubView('url-input');
                    else if (val === 'interview') {
                      setInterviewIdx(0);
                      setBrandSubView('interview');
                    } else if (val === 'import_pm') {
                      void handleSaveBrandPayload({importMarkdown: true}, 'Imported .agents/product-marketing.md!');
                    } else if (val === 'prompt') {
                      setBrandSubView('prompt-info');
                    } else if (val === 'skip') {
                      void handleSaveBrandPayload({skipBrand: true}, 'Set to Educational Mode (you can add a brand anytime via Ctrl+S).');
                    }
                  }}
                />
              </Box>
              <Box marginTop={1}>
                <Text color={theme.colors.mutedForeground} wrap="truncate-end">
                  ↑↓ move · Enter choose · Esc back to API Keys
                </Text>
              </Box>
            </>
          ) : null}

          {brandSubView === 'url-input' ? (
            <Box flexDirection="column">
              <Text bold wrap="truncate-end">
                Enter your company's website URL:
              </Text>
              <Text color={theme.colors.mutedForeground} wrap="truncate-end">
                Fleo will read llms.txt, sitemap & product pages to draft your ICP, voice, products, and competitors.
              </Text>
              <Box marginTop={1}>
                <TextInput
                  key="brand-url"
                  defaultValue={status?.brandSiteUrl ?? ''}
                  placeholder="e.g. https://linear.app or resend.com"
                  onSubmit={val => void handleScanUrl(val)}
                />
              </Box>
              <Box marginTop={1}>
                <Text color={theme.colors.mutedForeground} wrap="truncate-end">
                  Enter to scan · Esc back
                </Text>
              </Box>
            </Box>
          ) : null}

          {brandSubView === 'review-draft' && brandDraft ? (
            <Box flexDirection="column">
              <Text bold color={theme.colors.primary} wrap="truncate-end">
                Review Extracted Brand Profile (Press Enter on any row to tweak):
              </Text>
              <Box marginTop={1}>
                <OptionList
                  viewport={listViewport}
                  options={[
                    {value: 'save', label: `✓ Save "${brandDraft.company_name}" Profile & Continue →`},
                    {value: 'company_name', label: `Company:      ${brandDraft.company_name}`},
                    {value: 'category', label: `Category:     ${brandDraft.category}`},
                    {value: 'one_liner', label: `One-liner:    ${brandDraft.one_liner}`},
                    {value: 'icp_reader', label: `ICP Reader:   ${brandDraft.icp_reader}`},
                    {value: 'tone', label: `Brand Voice:  ${brandDraft.tone}`},
                    {value: 'competitors', label: `Competitors:  ${(brandDraft.direct_competitors || []).join(', ') || 'none'}`},
                  ]}
                  onSelect={val => {
                    if (val === 'save') {
                      void handleSaveBrandPayload({profile: brandDraft}, `Saved brand profile for ${brandDraft.company_name}!`);
                      return;
                    }
                    const labels: Record<string, string> = {
                      company_name: 'Company Name',
                      category: 'Category',
                      one_liner: 'One-Liner Description',
                      icp_reader: 'Target Reader (ICP)',
                      tone: 'Brand Voice / Tone',
                      competitors: 'Direct Competitors (comma-separated)',
                    };
                    if (labels[val]) {
                      setEditingBrandField({key: val as keyof BrandDraft, label: labels[val]});
                      setBrandSubView('edit-draft-field');
                    }
                  }}
                />
              </Box>
            </Box>
          ) : null}

          {brandSubView === 'edit-draft-field' && brandDraft && editingBrandField ? (
            <Box flexDirection="column">
              <Text bold wrap="truncate-end">
                Edit {editingBrandField.label}:
              </Text>
              <Box marginTop={1}>
                <TextInput
                  key={String(editingBrandField.key)}
                  defaultValue={
                    editingBrandField.key === 'competitors'
                      ? (brandDraft.direct_competitors || []).join(', ')
                      : String(brandDraft[editingBrandField.key] ?? '')
                  }
                  onSubmit={val => {
                    setBrandDraft(prev => {
                      if (!prev) return prev;
                      if (editingBrandField.key === 'competitors') {
                        return {
                          ...prev,
                          direct_competitors: val
                            .split(',')
                            .map(s => s.trim())
                            .filter(Boolean),
                        };
                      }
                      return {...prev, [editingBrandField.key]: val.trim()};
                    });
                    setEditingBrandField(null);
                    setBrandSubView('review-draft');
                  }}
                />
              </Box>
              <Box marginTop={1}>
                <Text color={theme.colors.mutedForeground} wrap="truncate-end">
                  Enter commit · Esc back
                </Text>
              </Box>
            </Box>
          ) : null}

          {brandSubView === 'interview' ? (
            <Box flexDirection="column">
              <Text bold color={theme.colors.primary} wrap="truncate-end">
                {INTERVIEW_QUESTIONS[interviewIdx]?.title}
              </Text>
              <Box marginTop={1}>
                <TextInput
                  key={`iv-${interviewIdx}`}
                  defaultValue={interviewAnswers[INTERVIEW_QUESTIONS[interviewIdx]?.key ?? ''] ?? ''}
                  placeholder={INTERVIEW_QUESTIONS[interviewIdx]?.placeholder}
                  onSubmit={val => {
                    const qKey = INTERVIEW_QUESTIONS[interviewIdx]?.key ?? 'company_name';
                    const nextAnswers = {...interviewAnswers, [qKey]: val.trim()};
                    setInterviewAnswers(nextAnswers);
                    if (interviewIdx + 1 < INTERVIEW_QUESTIONS.length) {
                      setInterviewIdx(interviewIdx + 1);
                    } else {
                      void handleSaveBrandPayload(
                        {interview: nextAnswers},
                        `Created brand profile for ${nextAnswers.company_name || 'your company'}!`,
                      );
                    }
                  }}
                />
              </Box>
              <Box marginTop={1}>
                <Text color={theme.colors.mutedForeground} wrap="truncate-end">
                  Enter next · Esc cancel interview
                </Text>
              </Box>
            </Box>
          ) : null}

          {brandSubView === 'prompt-info' ? (
            <Box flexDirection="column">
              <Text bold wrap="truncate-end">
                Generate Your Brand Context in Any AI Assistant:
              </Text>
              <Text color={theme.colors.mutedForeground} wrap="truncate-end">
                1. Open: {path.join(installationRoot, 'prompts', 'brand-context-builder.md')}
              </Text>
              <Text color={theme.colors.mutedForeground} wrap="truncate-end">
                2. Paste into Claude, ChatGPT, or Cursor & save to `.agents/product-marketing.md`.
              </Text>
              <Text color={theme.colors.mutedForeground} wrap="truncate-end">
                3. Fleo auto-loads `.agents/product-marketing.md` on every run!
              </Text>
              <Box marginTop={1}>
                <OptionList
                  options={[
                    {value: 'cont', label: '→ Continue to Website Stack Setup'},
                    {value: 'back', label: '← Back to Brand Options'},
                  ]}
                  onSelect={v => {
                    setBrandSubView('menu');
                    if (v === 'cont') setWizStep('target');
                  }}
                />
              </Box>
            </Box>
          ) : null}
        </Box>
      ) : null}

      {/*STEP 4: WEBSITE STACK & OUTPUT TARGET*/}
      {!busyMessage && wizStep === 'target' ? (
        <Box flexDirection="column">
          {targetSubView === 'menu' ? (
            <>
              <Text wrap="truncate-end">How do you want to publish finished SEO/AEO articles to your website?</Text>
              <Box marginTop={1}>
                <OptionList
                  viewport={listViewport}
                  options={[
                    {
                      value: 'handoff',
                      label: '✦ Agent Handoff Packet (agent-handoff.md) — Cursor, Claude Code, Lovable, Webflow, Framer (Recommended)',
                    },
                    {
                      value: 'repo',
                      label: '✦ Connect Local Website Git Repo [Experimental] — Direct file write (standard blogs only)',
                    },
                  ]}
                  onSelect={val => {
                    if (val === 'handoff') setTargetSubView('pick-stack');
                    else setTargetSubView('repo-path');
                  }}
                />
              </Box>
              <Box marginTop={1}>
                <Text color={theme.colors.mutedForeground} wrap="truncate-end">
                  ↑↓ move · Enter select · Esc back to Brand Setup
                </Text>
              </Box>
            </>
          ) : null}

          {targetSubView === 'pick-stack' ? (
            <Box flexDirection="column">
              <Text wrap="truncate-end">Which stack powers your website? (Tailors frontmatter, CMS exports & agent instructions):</Text>
              <Box marginTop={1}>
                <OptionList
                  viewport={listViewport}
                  options={[
                    ...STACK_OPTIONS.filter(o => o.value === detectedStackHint),
                    ...STACK_OPTIONS.filter(o => o.value !== detectedStackHint),
                  ]}
                  onSelect={fw => void handleSaveSiteTarget({mode: 'handoff', framework: fw})}
                />
              </Box>
              <Box marginTop={1}>
                <Text color={theme.colors.mutedForeground} wrap="truncate-end">
                  ↑↓ move · Enter confirm stack · Esc back
                </Text>
              </Box>
            </Box>
          ) : null}

          {targetSubView === 'repo-path' ? (
            <Box flexDirection="column">
              <StatusMessage tone="warning">
                Experimental: Direct repo publish only auto-detects standard blog folders and basic frontmatter keys.
              </StatusMessage>
              <Text color={theme.colors.mutedForeground} wrap="wrap">
                It does not read custom Zod/collection schemas, custom MDX components, or repo formatters. If unsure, press Esc and use Agent Handoff (agent-handoff.md), or clone Fleo and align it to your site repo using the agent prompt in README.md.
              </Text>
              <Box marginTop={1} flexDirection="column">
                <Text bold wrap="truncate-end">
                  Enter your website repo path (or paste the exact blog/content folder path):
                </Text>
                <Text color={theme.colors.mutedForeground} wrap="truncate-end">
                  Best-effort detection for Next.js / Astro / Nuxt / SvelteKit / Remix (never auto-commits or pushes).
                </Text>
              </Box>
              <Box marginTop={1}>
                <TextInput
                  key="repo-path-in"
                  defaultValue={pendingRepoPath}
                  placeholder="/path/to/your-website-repo (or /path/to/repo/src/content/guides)"
                  onSubmit={p => {
                    const trimmed = p.trim() || process.cwd();
                    setPendingRepoPath(trimmed);
                    setTargetSubView('content-dir');
                  }}
                />
              </Box>
              <Box marginTop={1}>
                <Text color={theme.colors.mutedForeground} wrap="truncate-end">
                  Enter next (optional folder override) · Esc back (use Agent Handoff)
                </Text>
              </Box>
            </Box>
          ) : null}

          {targetSubView === 'content-dir' ? (
            <Box flexDirection="column">
              <Text bold wrap="truncate-end">
                Optional: target folder path for published articles (leave blank to auto-detect):
              </Text>
              <Text color={theme.colors.mutedForeground} wrap="wrap">
                Repo path: {pendingRepoPath}. Enter a path relative to the repo (e.g. src/content/guides) or an absolute folder path, or press Enter to auto-detect (if you already pasted a content folder above, leave blank).
              </Text>
              <Box marginTop={1}>
                <TextInput
                  key="content-dir-in"
                  defaultValue=""
                  placeholder="src/content/guides (leave blank to auto-detect)"
                  onSubmit={dir =>
                    void handleSaveSiteTarget({
                      mode: 'repo',
                      repoPath: pendingRepoPath,
                      contentDir: dir.trim() || undefined,
                    })
                  }
                />
              </Box>
              <Box marginTop={1}>
                <Text color={theme.colors.mutedForeground} wrap="truncate-end">
                  Enter inspect & save target · Esc back to repo path
                </Text>
              </Box>
            </Box>
          ) : null}
        </Box>
      ) : null}

      {/*STEP 5: READY!*/}
      {!busyMessage && wizStep === 'ready' ? (
        <Box flexDirection="column">
          <StatusMessage tone="success">You are all set! Fleo is configured and ready to generate.</StatusMessage>
          <Box marginTop={1}>
            <KeyValue
              items={[
                ['Brand Profile', status?.brandConfigured ? `${status.brandName} (${status.brandSiteUrl || status.brandCategory})` : 'Educational / Unbranded Mode'],
                ['Website Target', status?.siteTarget?.frameworkLabel ?? 'Agent Handoff (.md)'],
                ...(status?.siteTarget?.contentDir &&
                (status.siteTarget.contentDirOverrideEnv || status.siteTarget.mode === 'repo')
                  ? [
                      [
                        'Publish Folder',
                        status.siteTarget.contentDirOverrideEnv
                          ? `${status.siteTarget.contentDir} (${status.siteTarget.contentDirOverrideEnv} override)`
                          : status.siteTarget.contentDir,
                      ] as [string, string],
                    ]
                  : []),
                [
                  'Pipeline Models',
                  customStepCount > 0 ? `${customStepCount} custom step(s) (${displayedTier})` : `Recommended Defaults (${displayedTier})`,
                ],
                ['Outputs Saved', 'Adapted post + @graph schema.json + agent-handoff.md'],
              ]}
            />
          </Box>
          <Box marginTop={1}>
            <OptionList
              options={[
                {value: 'start', label: '▶ Write Your First SEO/AEO Article'},
                {value: 'hub', label: 'Review Setup Hub'},
              ]}
              onSelect={v => {
                if (v === 'start') leaveSetup();
                else setWizStep('hub');
              }}
            />
          </Box>
        </Box>
      ) : null}
    </Box>
  );
};
