import {Text, useInput} from 'ink';
import React, {useCallback, useEffect, useReducer, useRef} from 'react';

// Drop-in replacement for @inkjs/ui's <TextInput> (same props at our call sites:
// defaultValue / placeholder / onChange / onSubmit) with one behavioral fix: it
// ignores ctrl/meta chords entirely. @inkjs/ui's input hook only special-cases
// ctrl+c, so every app chord that fires while an input is focused (Ctrl+T trends,
// Ctrl+G ideas, Ctrl+N fresh take) ALSO inserted its literal letter into the field
// — Ink delivers each keypress to every active useInput subscriber, so the global
// handler cannot stop the insertion; the input itself has to opt out.

type TextInputProps = {
  defaultValue?: string;
  placeholder?: string;
  mask?: boolean;
  onChange?: (value: string) => void;
  onSubmit?: (value: string) => void;
};

type InputState = {value: string; cursorOffset: number};

type InputAction =
  | {type: 'insert'; text: string}
  | {type: 'delete'}
  | {type: 'cursor-left'}
  | {type: 'cursor-right'};

const reducer = (state: InputState, action: InputAction): InputState => {
  switch (action.type) {
    case 'insert':
      return {
        value: state.value.slice(0, state.cursorOffset) + action.text + state.value.slice(state.cursorOffset),
        cursorOffset: state.cursorOffset + action.text.length,
      };
    case 'delete': {
      if (state.cursorOffset <= 0) return state;
      const next = state.cursorOffset - 1;
      return {value: state.value.slice(0, next) + state.value.slice(next + 1), cursorOffset: next};
    }
    case 'cursor-left':
      return {...state, cursorOffset: Math.max(0, state.cursorOffset - 1)};
    case 'cursor-right':
      return {...state, cursorOffset: Math.min(state.value.length, state.cursorOffset + 1)};
  }
};

const Value = ({value, cursorOffset, placeholder, mask = false}: InputState & {placeholder: string; mask?: boolean}) => {
  if (value.length === 0) {
    return placeholder.length > 0 ? (
      <Text>
        <Text inverse>{placeholder[0]}</Text>
        <Text dimColor>{placeholder.slice(1)}</Text>
      </Text>
    ) : (
      <Text inverse> </Text>
    );
  }
  const display = mask ? '•'.repeat(value.length) : value;
  const atEnd = cursorOffset >= display.length;
  return (
    <Text>
      {display.slice(0, cursorOffset)}
      {atEnd ? <Text inverse> </Text> : <Text inverse>{display[cursorOffset]}</Text>}
      {atEnd ? '' : display.slice(cursorOffset + 1)}
    </Text>
  );
};

export const TextInput = ({defaultValue = '', placeholder = '', mask = false, onChange, onSubmit}: TextInputProps) => {
  const [state, dispatch] = useReducer(reducer, {value: defaultValue, cursorOffset: defaultValue.length});

  const {value} = state;
  const mounted = useRef(false);
  useEffect(() => {
    // Match @inkjs/ui: no onChange for the initial defaultValue, only real edits.
    // Fire only on value changes — a new onChange identity alone must not re-emit.
    if (!mounted.current) {
      mounted.current = true;
      return;
    }
    onChange?.(value);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [value]);

  const handleInput = useCallback(
    (input: string, key: Parameters<Parameters<typeof useInput>[0]>[1]) => {
      if (key.ctrl || key.meta || key.escape || key.tab || key.upArrow || key.downArrow) return;
      if (key.return) {
        onSubmit?.(state.value);
        return;
      }
      if (key.leftArrow) return dispatch({type: 'cursor-left'});
      if (key.rightArrow) return dispatch({type: 'cursor-right'});
      if (key.backspace || key.delete) return dispatch({type: 'delete'});
      if (input.length > 0) dispatch({type: 'insert', text: input});
    },
    [onSubmit, state.value],
  );
  useInput(handleInput);

  return <Value value={state.value} cursorOffset={state.cursorOffset} placeholder={placeholder} mask={mask} />;
};
