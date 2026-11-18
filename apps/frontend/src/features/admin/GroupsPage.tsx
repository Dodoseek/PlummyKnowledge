import {
  Button,
  Card,
  CardBody,
  FormControl,
  FormErrorMessage,
  Heading,
  HStack,
  Input,
  Link,
  Stack,
  Table,
  TableContainer,
  Tbody,
  Td,
  Text,
  Th,
  Thead,
  Tr,
} from '@chakra-ui/react';
import { useState, type FormEvent } from 'react';
import { Link as RouterLink, useNavigate } from 'react-router';

import { toApiError } from '../../shared/api/errors';
import { fieldErrorsByName, messageFor } from '../../shared/api/messages';
import { ApiErrorAlert } from '../../shared/components/ApiErrorAlert';
import { PageSpinner } from '../../shared/components/PageSpinner';
import { PERMISSIONS, useHasPermission } from '../auth/permissions';
import { RequirePermission } from '../auth/RequirePermission';
import { useCreateGroupMutation, useListGroupsQuery } from './adminApi';
import { Pager } from './components';
import { useCursorPager } from './useCursorPager';

function CreateGroupForm() {
  const navigate = useNavigate();
  const [createGroup, { isLoading, error }] = useCreateGroupMutation();
  const [name, setName] = useState('');
  const apiError = error === undefined ? undefined : toApiError(error);
  const fields = fieldErrorsByName(apiError);

  const onSubmit = async (event: FormEvent) => {
    event.preventDefault();
    if (name.trim() === '') return;
    try {
      const group = await createGroup({ name: name.trim() }).unwrap();
      void navigate(`/admin/groups/${group.id}`);
    } catch {
      // Shown below.
    }
  };

  return (
    <form onSubmit={(event) => void onSubmit(event)} noValidate>
      <Stack spacing={2}>
        <HStack align="flex-start">
          <FormControl isInvalid={fields.name !== undefined}>
            <Input
              aria-label="New group name"
              placeholder="New group name"
              value={name}
              onChange={(event) => {
                setName(event.target.value);
              }}
            />
            <FormErrorMessage>{fields.name}</FormErrorMessage>
          </FormControl>
          <Button type="submit" colorScheme="brand" isLoading={isLoading} flexShrink={0}>
            Create group
          </Button>
        </HStack>
        {apiError !== undefined && fields.name === undefined && (
          <ApiErrorAlert error={{ ...apiError, message: messageFor(apiError) }} title="Group not created" />
        )}
      </Stack>
    </form>
  );
}

function GroupsList() {
  const canManage = useHasPermission(PERMISSIONS.groupsManage);
  const pager = useCursorPager();
  const [search, setSearch] = useState('');
  const [q, setQ] = useState('');
  const groups = useListGroupsQuery({ q, ...(pager.cursor !== undefined && { cursor: pager.cursor }) });

  return (
    <Stack spacing={6}>
      <Heading size="lg">Groups</Heading>
      {canManage && <CreateGroupForm />}
      <Card>
        <CardBody>
          <Stack spacing={4}>
            <form
              onSubmit={(event) => {
                event.preventDefault();
                setQ(search.trim());
                pager.reset();
              }}
            >
              <HStack>
                <Input
                  aria-label="Search groups"
                  placeholder="Search by name"
                  value={search}
                  onChange={(event) => {
                    setSearch(event.target.value);
                  }}
                />
                <Button type="submit">Search</Button>
              </HStack>
            </form>
            {groups.isLoading && <PageSpinner />}
            {groups.isError && <ApiErrorAlert error={toApiError(groups.error)} title="Cannot load groups" />}
            {groups.data !== undefined && (
              <>
                <TableContainer>
                  <Table size="sm">
                    <Thead>
                      <Tr>
                        <Th>Name</Th>
                        <Th>Description</Th>
                        <Th isNumeric>Members</Th>
                      </Tr>
                    </Thead>
                    <Tbody>
                      {groups.data.items.map((group) => (
                        <Tr key={group.id}>
                          <Td>
                            <Link as={RouterLink} to={`/admin/groups/${group.id}`} color="brand.500">
                              {group.name}
                            </Link>
                          </Td>
                          <Td maxW="sm" whiteSpace="normal">
                            <Text noOfLines={1}>{group.description ?? '—'}</Text>
                          </Td>
                          <Td isNumeric>{group.member_count}</Td>
                        </Tr>
                      ))}
                    </Tbody>
                  </Table>
                </TableContainer>
                {groups.data.items.length === 0 && <Text color="gray.500">No groups yet.</Text>}
                <Pager pager={pager} nextCursor={groups.data.next_cursor} />
              </>
            )}
          </Stack>
        </CardBody>
      </Card>
    </Stack>
  );
}

export function GroupsPage() {
  return (
    <RequirePermission permission={PERMISSIONS.groupsRead}>
      <GroupsList />
    </RequirePermission>
  );
}
