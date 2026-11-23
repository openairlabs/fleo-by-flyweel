import stringWidth from 'string-width';

// Word-wrap text into terminal-width lines. Ink has no native scrolling, so views
// pre-split long content with this and render a windowed slice. Width is measured
// in terminal cells and hard breaks happen only between Unicode grapheme clusters.
const segmenter = new Intl.Segmenter(undefined, {granularity: 'grapheme'});

const graphemes = (value: string): string[] => [...segmenter.segment(value)].map(part => part.segment);

const splitByCells = (value: string, width: number): string[] => {
  const chunks: string[] = [];
  let current = '';
  let cells = 0;
  for (const grapheme of graphemes(value)) {
    const nextCells = stringWidth(grapheme);
    if (current && cells + nextCells > width) {
      chunks.push(current);
      current = '';
      cells = 0;
    }
    current += grapheme;
    cells += nextCells;
  }
  if (current) chunks.push(current);
  return chunks;
};

export const wrapText = (text: string, width: number): string[] => {
  const safeWidth = Math.max(8, Math.floor(width));
  const lines: string[] = [];
  for (const raw of text.split(/\r?\n/)) {
    if (raw.trim() === '') {
      lines.push('');
      continue;
    }
    const rawIndent = /^\s*/.exec(raw)?.[0] ?? '';
    const indent = stringWidth(rawIndent) >= safeWidth - 1 ? '' : rawIndent;
    const contentWidth = Math.max(1, safeWidth - stringWidth(indent));
    let current = '';
    let currentCells = 0;

    for (const word of raw.trim().split(/\s+/)) {
      const wordCells = stringWidth(word);
      const separatorCells = current ? 1 : 0;
      if (current && currentCells + separatorCells + wordCells <= safeWidth) {
        current += ` ${word}`;
        currentCells += separatorCells + wordCells;
        continue;
      }
      if (current) {
        lines.push(current);
        current = '';
        currentCells = 0;
      }
      const chunks = splitByCells(word, contentWidth);
      for (const chunk of chunks.slice(0, -1)) lines.push(indent + chunk);
      const tail = chunks.at(-1) ?? '';
      current = indent + tail;
      currentCells = stringWidth(current);
    }
    if (current) lines.push(current);
  }
  return lines;
};
