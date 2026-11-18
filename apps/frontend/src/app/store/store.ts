import { combineReducers, configureStore } from '@reduxjs/toolkit';
import { setupListeners } from '@reduxjs/toolkit/query';

import { baseApi } from '../../shared/api/baseApi';
import { csrfSlice } from '../../shared/auth/csrfSlice';

const rootReducer = combineReducers({
  [baseApi.reducerPath]: baseApi.reducer,
  [csrfSlice.name]: csrfSlice.reducer,
});

export type RootState = ReturnType<typeof rootReducer>;

/** Factory (not a singleton) so tests get an isolated store and cache. */
export function makeStore(preloadedState?: Partial<RootState>) {
  const store = configureStore({
    reducer: rootReducer,
    middleware: (getDefaultMiddleware) => getDefaultMiddleware().concat(baseApi.middleware),
    ...(preloadedState !== undefined && { preloadedState }),
  });
  // Enables refetchOnFocus / refetchOnReconnect for queries that opt in.
  setupListeners(store.dispatch);
  return store;
}

export type AppStore = ReturnType<typeof makeStore>;
export type AppDispatch = AppStore['dispatch'];
