import '@testing-library/jest-dom/vitest';

import { cleanup } from '@testing-library/react';
import { afterEach } from 'vitest';

// Chakra UI color mode reads `prefers-color-scheme`; jsdom does not implement matchMedia.
Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: (query: string): MediaQueryList => ({
    matches: false,
    media: query,
    onchange: null,
    // Legacy API still called by some libraries.
    // eslint-disable-next-line @typescript-eslint/no-deprecated
    addListener: () => undefined,
    // eslint-disable-next-line @typescript-eslint/no-deprecated
    removeListener: () => undefined,
    addEventListener: () => undefined,
    removeEventListener: () => undefined,
    dispatchEvent: () => false,
  }),
});

afterEach(() => {
  cleanup();
});
