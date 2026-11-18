import { Navigate, Outlet, useLocation } from 'react-router';

import { toApiError } from '../../shared/api/errors';
import { ErrorWithRetry } from '../../shared/components/ErrorWithRetry';
import { PageSpinner } from '../../shared/components/PageSpinner';
import { useGetSetupStatusQuery } from '../setup/setupApi';
import { useGetSessionQuery } from './authApi';

/**
 * Route guard for pages that need a signed-in user. It only decides what to render: the backend checks the
 * session and permissions on every request and is the security boundary, not this component.
 */
export function RequireAuth() {
  const location = useLocation();
  const setup = useGetSetupStatusQuery();
  const initialized = setup.data?.status === 'initialized';
  const session = useGetSessionQuery(undefined, { skip: !initialized });

  if (setup.isLoading) {
    return <PageSpinner />;
  }
  if (setup.isError || setup.data === undefined) {
    return (
      <ErrorWithRetry
        error={toApiError(setup.error)}
        title="Cannot load the application state"
        onRetry={() => void setup.refetch()}
      />
    );
  }
  if (!initialized) {
    return <Navigate to="/setup" replace />;
  }
  if (session.isError) {
    const error = toApiError(session.error);
    if (error.status === 401) {
      const next = `${location.pathname}${location.search}`;
      const target = next === '/' ? '/login' : `/login?next=${encodeURIComponent(next)}`;
      return <Navigate to={target} replace />;
    }
    return (
      <ErrorWithRetry
        error={error}
        title="Cannot check your session"
        onRetry={() => void session.refetch()}
      />
    );
  }
  if (session.data === undefined) {
    return <PageSpinner />;
  }
  return <Outlet />;
}
