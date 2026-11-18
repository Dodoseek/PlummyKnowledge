import type { RouteObject } from 'react-router';

import { AccountPage } from '../features/account/AccountPage';
import { GroupDetailPage } from '../features/admin/GroupDetailPage';
import { GroupsPage } from '../features/admin/GroupsPage';
import { UserDetailPage } from '../features/admin/UserDetailPage';
import { UsersPage } from '../features/admin/UsersPage';
import { ActivatePage } from '../features/auth/ActivatePage';
import { LoginPage } from '../features/auth/LoginPage';
import { RequireAuth } from '../features/auth/RequireAuth';
import { EmbeddingProfilesPage } from '../features/providers/EmbeddingProfilesPage';
import { ProviderDetailPage } from '../features/providers/ProviderDetailPage';
import { ProvidersPage } from '../features/providers/ProvidersPage';
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
      { path: 'activate', element: <ActivatePage /> },
      {
        // Everything below needs a signed-in user (the backend enforces it on every request).
        element: <RequireAuth />,
        children: [
          { index: true, element: <HomePage /> },
          { path: 'account', element: <AccountPage /> },
          // Admin pages hide themselves without the permission; the backend enforces it on every request.
          { path: 'admin/users', element: <UsersPage /> },
          { path: 'admin/users/:userId', element: <UserDetailPage /> },
          { path: 'admin/groups', element: <GroupsPage /> },
          { path: 'admin/groups/:groupId', element: <GroupDetailPage /> },
          { path: 'admin/providers', element: <ProvidersPage /> },
          { path: 'admin/providers/:providerId', element: <ProviderDetailPage /> },
          { path: 'admin/embedding-profiles', element: <EmbeddingProfilesPage /> },
        ],
      },
      { path: '*', element: <NotFoundPage /> },
    ],
  },
];
