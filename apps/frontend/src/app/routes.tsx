import type { RouteObject } from 'react-router';

import { LoginPage } from '../features/auth/LoginPage';
import { RequireAuth } from '../features/auth/RequireAuth';
import { SetupPage } from '../features/setup/SetupPage';
import { AppLayout } from '../layouts/AppLayout';
import { HomePage } from '../pages/HomePage';
import { NotFoundPage } from '../pages/NotFoundPage';
import { RouteErrorPage } from '../pages/RouteErrorPage';

/** Route table shared by the browser router and tests (memory router). */
export const routes: RouteObject[] = [
  {
    path: '/',
    element: <AppLayout />,
    errorElement: <RouteErrorPage />,
    children: [
      { path: 'setup', element: <SetupPage /> },
      { path: 'login', element: <LoginPage /> },
      {
        // Everything below needs a signed-in user (the backend enforces it on every request).
        element: <RequireAuth />,
        children: [{ index: true, element: <HomePage /> }],
      },
      { path: '*', element: <NotFoundPage /> },
    ],
  },
];
