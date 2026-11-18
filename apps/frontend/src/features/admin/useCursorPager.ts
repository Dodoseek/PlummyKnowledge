import { useState } from 'react';

/** Cursor pager: keeps the cursors of visited pages to go back (ADR-014). */
export function useCursorPager() {
  const [cursors, setCursors] = useState<string[]>([]);
  return {
    cursor: cursors.at(-1),
    page: cursors.length + 1,
    next: (cursor: string) => {
      setCursors((stack) => [...stack, cursor]);
    },
    previous: () => {
      setCursors((stack) => stack.slice(0, -1));
    },
    reset: () => {
      setCursors([]);
    },
  };
}

export type CursorPager = ReturnType<typeof useCursorPager>;
