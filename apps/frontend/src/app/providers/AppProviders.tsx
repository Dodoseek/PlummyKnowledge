import { ChakraProvider } from '@chakra-ui/react';
import { useState, type ReactNode } from 'react';
import { Provider as ReduxProvider } from 'react-redux';

import { makeStore, type AppStore } from '../store/store';
import { theme } from '../theme';

interface AppProvidersProps {
  children: ReactNode;
  /** Injected in tests; the app creates its own store once. */
  store?: AppStore;
}

export function AppProviders({ children, store }: AppProvidersProps) {
  const [appStore] = useState(() => store ?? makeStore());
  return (
    <ReduxProvider store={appStore}>
      <ChakraProvider theme={theme}>{children}</ChakraProvider>
    </ReduxProvider>
  );
}
