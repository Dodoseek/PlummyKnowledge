import { Alert, AlertDescription, AlertIcon } from '@chakra-ui/react';
import type { ReactNode } from 'react';

import { useHasPermission } from './permissions';

/**
 * Shows the page only to users whose session has the permission. This is a convenience for the UI: the
 * backend rejects the requests anyway (`403 permission_denied`).
 */
export function RequirePermission({ permission, children }: { permission: string; children: ReactNode }) {
  if (!useHasPermission(permission)) {
    return (
      <Alert status="warning" borderRadius="md" role="alert">
        <AlertIcon />
        <AlertDescription>You do not have permission to view this page.</AlertDescription>
      </Alert>
    );
  }
  return <>{children}</>;
}
