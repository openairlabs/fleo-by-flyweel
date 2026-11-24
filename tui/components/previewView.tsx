import React, {useEffect, useMemo, useState} from 'react';
import {Box, Text, useInput} from 'ink';
import {Spinner} from '@inkjs/ui';

import {useLayout} from '../lib/useTerminalSize.js';
import {wrapText} from '../lib/wrap.js';
import {buildPreviewModel} from '../preview.js';
import {KeyValue} from './termcn.js';
import {useTheme} from './ui/theme-provider.js';
import type {AppState} from '../types.js';

// Rows the summary block, spinners, and the line counter claim out of the body budget.
const CHROME_OVERHEAD = 9;

const compactValue = (value: unknown): string => {
  if (value === null || value === undefined) return '';
  if (typeof value === 'number') return Number.isInteger(value) ? String(value) : value.toFixed(1);
  if (typeof value === 'string') return value;
  if (typeof value === 'boolean') return value ? 'yes' : 'no';
  return '';
};

// Human summary of the generated article: fixed header + scrollable body. No raw
// JSON dumps — metadata/raw draft live on disk and in the log drawer.
export const PreviewView = ({state, active}: {state: AppState; active: boolean}) => {
  const theme = useTheme();
  const {columns, bodyRows} = useLayout();
  const [offset, setOffset] = useState(0);
  const [showInfo, setShowInfo] = useState(false);
  // Ctrl+O (alias Ctrl+P): hide the summary chrome and give the scroller the whole panel.
  const [fullArticle, setFullArticle] = useState(false);

  const preview = useMemo(() => buildPreviewModel(state), [state.artifacts, state.qualityMetrics, state.generationSummary]);
  const lines = useMemo(() => (preview.article.trim() ? wrapText(preview.article, Math.max(20, columns - 6)) : []), [preview.article, columns]);
  const quality = preview.quality;
  const composite = compactValue(quality.composite ?? quality.compositeScore ?? quality.overall);
  const namedQuality = useMemo(
    () =>
      Object.entries(quality)
        .filter(([key, value]) => key !== 'composite' && (typeof value === 'number' || typeof value === 'string')),
    [quality],
  );
  const infoItems = useMemo(() => {
    const out: Array<{key: string; bold?: boolean; dim?: boolean; text: string}> = [{key: 'cit-hdr', bold: true, text: 'Citations'}];
    if (preview.citations.length > 0) {
      preview.citations.forEach((citation, index) => {
        out.push({key: `cit-${index}`, dim: true, text: citation});
      });
    } else {
      out.push({key: 'cit-empty', dim: true, text: 'No citation links detected.'});
    }
    out.push({key: 'qual-hdr', bold: true, text: 'Quality'});
    if (namedQuality.length > 0) {
      namedQuality.forEach(([key, value]) => {
        out.push({key: `qual-${key}`, dim: true, text: `${key}: ${compactValue(value)}`});
      });
    } else {
      out.push({key: 'qual-empty', dim: true, text: 'No quality fields reported.'});
    }
    return out;
  }, [preview.citations, namedQuality]);

  const viewport = fullArticle
    ? Math.max(4, bodyRows - 2)
    : showInfo
      ? Math.max(3, bodyRows - CHROME_OVERHEAD - 2)
      : Math.max(4, bodyRows - CHROME_OVERHEAD);
  const activeLength = showInfo && !fullArticle ? infoItems.length : lines.length;
  const maxOffset = Math.max(0, activeLength - viewport);

  // Clamp the window when the terminal resizes or a new article arrives.
  useEffect(() => {
    setOffset(current => Math.min(current, maxOffset));
  }, [maxOffset]);

  useInput(
    (input, key) => {
      // ^O is the documented full-content key (shared with the review step); ^P
      // stays as a silent alias from the first release.
      if (key.ctrl && (input === 'o' || input === 'p')) {
        setFullArticle(current => !current);
        return;
      }
      // Esc leaves full-article mode only — it never exits the preview step.
      if (key.escape && fullArticle) {
        setFullArticle(false);
        return;
      }
      if (key.upArrow) setOffset(current => Math.max(0, current - 1));
      if (key.downArrow) setOffset(current => Math.min(maxOffset, current + 1));
      if (key.pageUp) setOffset(current => Math.max(0, current - viewport));
      if (key.pageDown) setOffset(current => Math.min(maxOffset, current + viewport));
      if (input === 'g') setOffset(0);
      if (input === 'G') setOffset(maxOffset);
      if (input === 'i' && !fullArticle) {
        setOffset(0);
        setShowInfo(current => !current);
      }
    },
    {isActive: active},
  );

  const summaryItems: Array<[string, React.ReactNode]> = [
    ['Title', preview.title],
    ['Slug', preview.slug],
    ['Style', state.config.style],
    ['Quality', composite ? `${composite} composite` : 'not scored'],
    ['Citations', String(preview.citations.length)],
  ];

  const visible = lines.slice(offset, offset + viewport);
  const visibleInfo = infoItems.slice(offset, offset + viewport);

  if (fullArticle) {
    return (
      <Box flexDirection="column">
        {visible.length > 0 ? (
          visible.map((line, index) => <Text key={`full-${offset + index}`}>{line || ' '}</Text>)
        ) : (
          <Text dimColor>Final article body appears here after generation.</Text>
        )}
        <Text color={theme.colors.mutedForeground}>
          lines {lines.length === 0 ? 0 : offset + 1}–{Math.min(lines.length, offset + viewport)} of {lines.length} · Ctrl+O or Esc to exit full article
        </Text>
      </Box>
    );
  }

  return (
    <Box flexDirection="column">
      <KeyValue items={summaryItems} />
      {state.saveState === 'running' ? <Spinner label="Saving locally…" /> : null}
      {state.publishState === 'running' ? <Spinner label="Publishing to connected site…" /> : null}
      <Box marginTop={1} flexDirection="column">
        {showInfo ? (
          <>
            {visibleInfo.map(item => (
              <Text key={item.key} bold={item.bold} dimColor={item.dim} wrap="truncate-end">
                {item.text}
              </Text>
            ))}
            <Box marginTop={1}>
              <Text color={theme.colors.mutedForeground}>
                {infoItems.length > viewport
                  ? `items ${offset + 1}–${Math.min(infoItems.length, offset + viewport)} of ${infoItems.length} · ↑↓ scroll · `
                  : ''}
                i to return to the article
              </Text>
            </Box>
          </>
        ) : (
          <>
            {visible.length > 0 ? (
              visible.map((line, index) => <Text key={`line-${offset + index}`}>{line || ' '}</Text>)
            ) : (
              <Text dimColor>Final article body appears here after generation.</Text>
            )}
            {lines.length > viewport ? (
              <Box marginTop={1}>
                <Text color={theme.colors.mutedForeground}>
                  lines {offset + 1}–{Math.min(lines.length, offset + viewport)} of {lines.length}
                </Text>
              </Box>
            ) : null}
          </>
        )}
      </Box>
    </Box>
  );
};
