import {
  Button,
  Card,
  CardBody,
  CardHeader,
  FormControl,
  FormErrorMessage,
  FormLabel,
  Heading,
  HStack,
  IconButton,
  Input,
  Link,
  List,
  ListItem,
  Select,
  Stack,
  Text,
  Textarea,
  useToast,
} from '@chakra-ui/react';
import { useState } from 'react';
import { Link as RouterLink, useNavigate, useParams } from 'react-router';

import { toApiError, type ApiError } from '../../shared/api/errors';
import { fieldErrorsByName, messageFor } from '../../shared/api/messages';
import { ApiErrorAlert } from '../../shared/components/ApiErrorAlert';
import { PageSpinner } from '../../shared/components/PageSpinner';
import { PERMISSIONS, useHasPermission } from '../auth/permissions';
import { RequirePermission } from '../auth/RequirePermission';
import {
  useDeleteGroupMutation,
  useGetGroupQuery,
  useListRolesQuery,
  useListUsersQuery,
  useSetGroupMembersMutation,
  useSetGroupRolesMutation,
  useUpdateGroupMutation,
  type GroupDetails,
} from './adminApi';
import { ConfirmButton, RoleCheckboxes, StatusBadge } from './components';

/** Granted to users directly only (the last-administrator rule depends on it). */
const NOT_FOR_GROUPS = ['admin'];

type Run = (action: () => Promise<unknown>, success?: string) => Promise<void>;

function DetailsCard({ group, run }: { group: GroupDetails; run: Run }) {
  const [updateGroup, { isLoading, error }] = useUpdateGroupMutation();
  const [name, setName] = useState(group.name);
  const [description, setDescription] = useState(group.description ?? '');
  const fields = fieldErrorsByName(error === undefined ? undefined : toApiError(error));

  return (
    <Card>
      <CardHeader pb={0}>
        <Heading size="md">Details</Heading>
      </CardHeader>
      <CardBody>
        <Stack spacing={4} maxW="lg">
          <FormControl isInvalid={fields.name !== undefined}>
            <FormLabel>Name</FormLabel>
            <Input
              value={name}
              onChange={(event) => {
                setName(event.target.value);
              }}
            />
            <FormErrorMessage>{fields.name}</FormErrorMessage>
          </FormControl>
          <FormControl isInvalid={fields.description !== undefined}>
            <FormLabel>Description</FormLabel>
            <Textarea
              value={description}
              onChange={(event) => {
                setDescription(event.target.value);
              }}
            />
            <FormErrorMessage>{fields.description}</FormErrorMessage>
          </FormControl>
          <Button
            size="sm"
            alignSelf="flex-start"
            isLoading={isLoading}
            onClick={() =>
              void run(
                () =>
                  updateGroup({
                    id: group.id,
                    name: name.trim(),
                    description: description.trim() === '' ? null : description,
                  }).unwrap(),
                'Group saved.',
              )
            }
          >
            Save
          </Button>
        </Stack>
      </CardBody>
    </Card>
  );
}

function MembersCard({ group, run, canManage }: { group: GroupDetails; run: Run; canManage: boolean }) {
  const [search, setSearch] = useState('');
  const [q, setQ] = useState('');
  const [candidate, setCandidate] = useState('');
  // Picking users needs `users.read` as well; without it only removal is offered.
  const canReadUsers = useHasPermission(PERMISSIONS.usersRead);
  const candidates = useListUsersQuery({ q, limit: 50 }, { skip: !canManage || !canReadUsers });
  const [setMembers, { isLoading }] = useSetGroupMembersMutation();
  const memberIds = group.members.map((member) => member.id);

  const save = (ids: string[]) => run(() => setMembers({ id: group.id, user_ids: ids }).unwrap());

  return (
    <Card>
      <CardHeader pb={0}>
        <Heading size="md">Members ({group.members.length})</Heading>
      </CardHeader>
      <CardBody>
        <Stack spacing={4}>
          <List spacing={2}>
            {group.members.map((member) => (
              <ListItem key={member.id}>
                <HStack justify="space-between">
                  <HStack>
                    <Text>{member.display_name}</Text>
                    <Text color="gray.500" fontSize="sm">
                      {member.email}
                    </Text>
                    <StatusBadge status={member.status} />
                  </HStack>
                  {canManage && (
                    <IconButton
                      size="xs"
                      variant="ghost"
                      aria-label={`Remove ${member.email}`}
                      icon={<span aria-hidden>✕</span>}
                      isDisabled={isLoading}
                      onClick={() => void save(memberIds.filter((id) => id !== member.id))}
                    />
                  )}
                </HStack>
              </ListItem>
            ))}
          </List>
          {group.members.length === 0 && <Text color="gray.500">No members yet.</Text>}
          {canManage && canReadUsers && (
            <Stack spacing={2}>
              <HStack>
                <Input
                  aria-label="Find users to add"
                  placeholder="Find users by email or name"
                  value={search}
                  onChange={(event) => {
                    setSearch(event.target.value);
                  }}
                  onKeyDown={(event) => {
                    if (event.key === 'Enter') setQ(search.trim());
                  }}
                />
                <Button
                  onClick={() => {
                    setQ(search.trim());
                  }}
                >
                  Find
                </Button>
              </HStack>
              <HStack>
                <Select
                  aria-label="User to add"
                  placeholder="Select a user"
                  value={candidate}
                  onChange={(event) => {
                    setCandidate(event.target.value);
                  }}
                >
                  {(candidates.data?.items ?? [])
                    .filter((user) => !memberIds.includes(user.id))
                    .map((user) => (
                      <option key={user.id} value={user.id}>
                        {user.display_name} ({user.email})
                      </option>
                    ))}
                </Select>
                <Button
                  colorScheme="brand"
                  isDisabled={candidate === ''}
                  isLoading={isLoading}
                  onClick={() =>
                    void save([...memberIds, candidate]).then(() => {
                      setCandidate('');
                    })
                  }
                >
                  Add
                </Button>
              </HStack>
            </Stack>
          )}
        </Stack>
      </CardBody>
    </Card>
  );
}

function RolesCard({ group, run }: { group: GroupDetails; run: Run }) {
  const roles = useListRolesQuery();
  const [setRoles, { isLoading }] = useSetGroupRolesMutation();
  const [selected, setSelected] = useState(group.roles);

  return (
    <Card>
      <CardHeader pb={0}>
        <Heading size="md">Roles</Heading>
        <Text fontSize="sm" color="gray.500">
          Every member gets these roles. Administrator can only be granted to a user directly.
        </Text>
      </CardHeader>
      <CardBody>
        <Stack spacing={4}>
          {roles.data !== undefined && (
            <RoleCheckboxes
              roles={roles.data.items}
              value={selected}
              onChange={setSelected}
              exclude={NOT_FOR_GROUPS}
            />
          )}
          <Button
            size="sm"
            alignSelf="flex-start"
            isLoading={isLoading}
            onClick={() =>
              void run(() => setRoles({ id: group.id, roles: selected }).unwrap(), 'Roles saved.')
            }
          >
            Save roles
          </Button>
        </Stack>
      </CardBody>
    </Card>
  );
}

function GroupDetail({ id }: { id: string }) {
  const navigate = useNavigate();
  const toast = useToast();
  const canManage = useHasPermission(PERMISSIONS.groupsManage);
  const canAssign = useHasPermission(PERMISSIONS.rolesAssign);
  const group = useGetGroupQuery(id);
  const [deleteGroup, deletion] = useDeleteGroupMutation();
  const [error, setError] = useState<ApiError | null>(null);

  if (group.isLoading) return <PageSpinner />;
  if (group.isError || group.data === undefined) {
    return <ApiErrorAlert error={toApiError(group.error)} title="Cannot load the group" />;
  }
  const details = group.data;

  const run: Run = async (action, success) => {
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
      <Link as={RouterLink} to="/admin/groups" color="brand.500" fontSize="sm">
        ← All groups
      </Link>
      <HStack justify="space-between">
        <Heading size="lg">{details.name}</Heading>
        {canManage && (
          <ConfirmButton
            label="Delete group"
            confirmLabel="Confirm delete"
            isLoading={deletion.isLoading}
            onConfirm={() =>
              void run(async () => {
                await deleteGroup(details.id).unwrap();
                void navigate('/admin/groups', { replace: true });
              })
            }
          />
        )}
      </HStack>
      {details.description !== null && <Text color="gray.500">{details.description}</Text>}
      {error !== null && (
        <ApiErrorAlert error={{ ...error, message: messageFor(error) }} title="Action failed" />
      )}
      {/* Forms remount with the saved values as their new starting point. */}
      {canManage && (
        <DetailsCard key={`${details.name}|${details.description ?? ''}`} group={details} run={run} />
      )}
      <MembersCard group={details} run={run} canManage={canManage} />
      {canAssign && <RolesCard key={details.roles.join(',')} group={details} run={run} />}
    </Stack>
  );
}

export function GroupDetailPage() {
  const { groupId = '' } = useParams();
  return (
    <RequirePermission permission={PERMISSIONS.groupsRead}>
      <GroupDetail id={groupId} />
    </RequirePermission>
  );
}
