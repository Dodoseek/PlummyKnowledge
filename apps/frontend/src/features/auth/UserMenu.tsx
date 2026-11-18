import { Button, HStack, Link } from '@chakra-ui/react';
import { Link as RouterLink, useNavigate } from 'react-router';

import { authApi, useLogoutMutation } from './authApi';
import { PERMISSIONS } from './permissions';

/** Header block with the signed-in user. Reads the cached session only; it never triggers a request. */
export function UserMenu() {
  const navigate = useNavigate();
  const { data: session, isError } = authApi.endpoints.getSession.useQueryState();
  const [logout, { isLoading }] = useLogoutMutation();

  if (session === undefined || isError) {
    return null;
  }

  // Links are hidden without the permission; the pages and the API check it again.
  const can = (permission: string) => session.permissions.includes(permission);

  const onLogout = async () => {
    try {
      await logout().unwrap();
    } finally {
      void navigate('/login', { replace: true });
    }
  };

  return (
    <HStack spacing={4} as="nav" aria-label="Main">
      {can(PERMISSIONS.usersRead) && (
        <Link as={RouterLink} to="/admin/users" fontSize="sm">
          Users
        </Link>
      )}
      {can(PERMISSIONS.groupsRead) && (
        <Link as={RouterLink} to="/admin/groups" fontSize="sm">
          Groups
        </Link>
      )}
      {can(PERMISSIONS.providersRead) && (
        <Link as={RouterLink} to="/admin/providers" fontSize="sm">
          AI providers
        </Link>
      )}
      <Link as={RouterLink} to="/account" fontSize="sm" data-testid="current-user">
        {session.user.display_name}
      </Link>
      <Button size="sm" variant="outline" onClick={() => void onLogout()} isLoading={isLoading}>
        Sign out
      </Button>
    </HStack>
  );
}
