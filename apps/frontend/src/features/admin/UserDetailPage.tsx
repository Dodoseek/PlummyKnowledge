import {
  Button,
  Card,
  CardBody,
  CardHeader,
  Heading,
  HStack,
  Link,
  Stack,
  Text,
  useToast,
  Wrap,
  WrapItem,
} from '@chakra-ui/react';
import { useState } from 'react';
import { Link as RouterLink, useNavigate, useParams } from 'react-router';

import { toApiError, type ApiError } from '../../shared/api/errors';
import { messageFor } from '../../shared/api/messages';
import { ApiErrorAlert } from '../../shared/components/ApiErrorAlert';
import { PageSpinner } from '../../shared/components/PageSpinner';
import { authApi } from '../auth/authApi';
import { PERMISSIONS, useHasPermission } from '../auth/permissions';
import { RequirePermission } from '../auth/RequirePermission';
import {
  useDeleteUserMutation,
  useGetUserQuery,
  useListRolesQuery,
  useReissueActivationMutation,
  useRevokeUserSessionsMutation,
  useSetUserRolesMutation,
  useUpdateUserMutation,
  type ActivationLink,
  type UserDetails,
} from './adminApi';
import { ActivationLinkNotice, ConfirmButton, RoleCheckboxes, StatusBadge } from './components';

function RolesCard({ user }: { user: UserDetails }) {
  const toast = useToast();
  const roles = useListRolesQuery();
  const [setRoles, { isLoading }] = useSetUserRolesMutation();
  const [selected, setSelected] = useState(user.roles);
  const [error, setError] = useState<ApiError | null>(null);

  const save = async () => {
    setError(null);
    try {
      await setRoles({ id: user.id, roles: selected }).unwrap();
      toast({ status: 'success', title: 'Roles saved.' });
    } catch (caught) {
      setError(toApiError(caught as Parameters<typeof toApiError>[0]));
    }
  };

  return (
    <Card>
      <CardHeader pb={0}>
        <Heading size="md">Roles</Heading>
        <Text fontSize="sm" color="gray.500">
          Direct roles. Roles of the user’s groups apply as well.
        </Text>
      </CardHeader>
      <CardBody>
        <Stack spacing={4}>
          {error !== null && (
            <ApiErrorAlert error={{ ...error, message: messageFor(error) }} title="Roles not saved" />
          )}
          {roles.data !== undefined && (
            <RoleCheckboxes roles={roles.data.items} value={selected} onChange={setSelected} />
          )}
          <Button size="sm" alignSelf="flex-start" onClick={() => void save()} isLoading={isLoading}>
            Save roles
          </Button>
        </Stack>
      </CardBody>
    </Card>
  );
}

function UserDetail({ id }: { id: string }) {
  const navigate = useNavigate();
  const toast = useToast();
  const { data: session } = authApi.endpoints.getSession.useQueryState();
  const canManage = useHasPermission(PERMISSIONS.usersManage);
  const canAssign = useHasPermission(PERMISSIONS.rolesAssign);
  const user = useGetUserQuery(id);
  const [updateUser, update] = useUpdateUserMutation();
  const [deleteUser, deletion] = useDeleteUserMutation();
  const [revokeSessions, revocation] = useRevokeUserSessionsMutation();
  const [reissue, reissuing] = useReissueActivationMutation();
  const [link, setLink] = useState<ActivationLink | null>(null);
  const [error, setError] = useState<ApiError | null>(null);

  if (user.isLoading) return <PageSpinner />;
  if (user.isError || user.data === undefined) {
    return <ApiErrorAlert error={toApiError(user.error)} title="Cannot load the user" />;
  }
  const details = user.data;
  const isSelf = session?.user.id === details.id;

  const run = async (action: () => Promise<unknown>, success?: string) => {
    setError(null);
    try {
      await action();
      if (success !== undefined) toast({ status: 'success', title: success });
    } catch (caught) {
      setError(toApiError(caught as Parameters<typeof toApiError>[0]));
    }
  };

  return (
    <Stack spacing={6}>
      <Link as={RouterLink} to="/admin/users" color="brand.500" fontSize="sm">
        ← All users
      </Link>
      <Stack spacing={1}>
        <HStack>
          <Heading size="lg">{details.display_name}</Heading>
          <StatusBadge status={details.status} />
        </HStack>
        <Text color="gray.500">{details.email}</Text>
        <Text fontSize="sm" color="gray.500">
          Groups: {details.groups.map((group) => group.name).join(', ') || 'none'}
        </Text>
      </Stack>
      {error !== null && (
        <ApiErrorAlert error={{ ...error, message: messageFor(error) }} title="Action failed" />
      )}
      {link !== null && <ActivationLinkNotice link={link} />}
      {canManage && (
        <Card>
          <CardHeader pb={0}>
            <Heading size="md">Account</Heading>
          </CardHeader>
          <CardBody>
            <Wrap spacing={3}>
              {details.status === 'invited' && (
                <WrapItem>
                  <Button
                    size="sm"
                    isLoading={reissuing.isLoading}
                    onClick={() =>
                      void run(async () => {
                        setLink(await reissue(details.id).unwrap());
                      })
                    }
                  >
                    New activation link
                  </Button>
                </WrapItem>
              )}
              {details.status !== 'disabled' && !isSelf && (
                <WrapItem>
                  <Button
                    size="sm"
                    isLoading={update.isLoading}
                    onClick={() =>
                      void run(
                        () => updateUser({ id: details.id, status: 'disabled' }).unwrap(),
                        'User disabled.',
                      )
                    }
                  >
                    Disable
                  </Button>
                </WrapItem>
              )}
              {details.status === 'disabled' && (
                <WrapItem>
                  <Button
                    size="sm"
                    isLoading={update.isLoading}
                    onClick={() =>
                      void run(
                        () => updateUser({ id: details.id, status: 'active' }).unwrap(),
                        'User enabled.',
                      )
                    }
                  >
                    Enable
                  </Button>
                </WrapItem>
              )}
              <WrapItem>
                <Button
                  size="sm"
                  isLoading={revocation.isLoading}
                  onClick={() =>
                    void run(async () => {
                      const result = await revokeSessions(details.id).unwrap();
                      toast({ status: 'success', title: `Ended ${result.revoked} session(s).` });
                    })
                  }
                >
                  Sign out everywhere
                </Button>
              </WrapItem>
              {!isSelf && (
                <WrapItem>
                  <ConfirmButton
                    label="Delete user"
                    confirmLabel="Confirm delete"
                    isLoading={deletion.isLoading}
                    onConfirm={() =>
                      void run(async () => {
                        await deleteUser(details.id).unwrap();
                        void navigate('/admin/users', { replace: true });
                      })
                    }
                  />
                </WrapItem>
              )}
            </Wrap>
          </CardBody>
        </Card>
      )}
      {/* Remount with the saved roles as the new starting point. */}
      {canAssign && <RolesCard key={details.roles.join(',')} user={details} />}
    </Stack>
  );
}

export function UserDetailPage() {
  const { userId = '' } = useParams();
  return (
    <RequirePermission permission={PERMISSIONS.usersRead}>
      <UserDetail id={userId} />
    </RequirePermission>
  );
}
