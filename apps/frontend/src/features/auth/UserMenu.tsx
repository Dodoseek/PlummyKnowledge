import { Button, HStack, Text } from '@chakra-ui/react';
import { useNavigate } from 'react-router';

import { authApi, useLogoutMutation } from './authApi';

/** Header block with the signed-in user. Reads the cached session only; it never triggers a request. */
export function UserMenu() {
  const navigate = useNavigate();
  const { data: session, isError } = authApi.endpoints.getSession.useQueryState();
  const [logout, { isLoading }] = useLogoutMutation();

  if (session === undefined || isError) {
    return null;
  }

  const onLogout = async () => {
    try {
      await logout().unwrap();
    } finally {
      void navigate('/login', { replace: true });
    }
  };

  return (
    <HStack spacing={3}>
      <Text fontSize="sm" data-testid="current-user">
        {session.user.display_name}
      </Text>
      <Button size="sm" variant="outline" onClick={() => void onLogout()} isLoading={isLoading}>
        Sign out
      </Button>
    </HStack>
  );
}
