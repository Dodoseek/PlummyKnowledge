import {
  Button,
  Card,
  CardBody,
  FormControl,
  FormErrorMessage,
  FormLabel,
  Heading,
  HStack,
  Input,
  Link,
  Modal,
  ModalBody,
  ModalCloseButton,
  ModalContent,
  ModalFooter,
  ModalHeader,
  ModalOverlay,
  Select,
  Stack,
  Table,
  TableContainer,
  Tbody,
  Td,
  Text,
  Th,
  Thead,
  Tr,
  useDisclosure,
} from '@chakra-ui/react';
import { useState, type FormEvent } from 'react';
import { Link as RouterLink } from 'react-router';

import { toApiError } from '../../shared/api/errors';
import { fieldErrorsByName, messageFor } from '../../shared/api/messages';
import { ApiErrorAlert } from '../../shared/components/ApiErrorAlert';
import { PageSpinner } from '../../shared/components/PageSpinner';
import { PERMISSIONS, useHasPermission } from '../auth/permissions';
import { RequirePermission } from '../auth/RequirePermission';
import {
  useCreateUserMutation,
  useListRolesQuery,
  useListUsersQuery,
  type ActivationLink,
  type UserStatus,
} from './adminApi';
import { ActivationLinkNotice, Pager, RoleCheckboxes, StatusBadge } from './components';
import { useCursorPager } from './useCursorPager';

const PAGE_SIZE = 25;

function CreateUserModal({ isOpen, onClose }: { isOpen: boolean; onClose: () => void }) {
  const canAssign = useHasPermission(PERMISSIONS.rolesAssign);
  const roles = useListRolesQuery(undefined, { skip: !isOpen || !canAssign });
  const [createUser, { isLoading, error, reset }] = useCreateUserMutation();
  const [email, setEmail] = useState('');
  const [name, setName] = useState('');
  const [selected, setSelected] = useState<string[]>(['member']);
  const [link, setLink] = useState<ActivationLink | null>(null);

  const apiError = error === undefined ? undefined : toApiError(error);
  const fields = fieldErrorsByName(apiError);

  const close = () => {
    setEmail('');
    setName('');
    setSelected(['member']);
    setLink(null);
    reset();
    onClose();
  };

  const onSubmit = async (event: FormEvent) => {
    event.preventDefault();
    try {
      const created = await createUser({
        email: email.trim(),
        display_name: name.trim(),
        roles: canAssign ? selected : [],
      }).unwrap();
      setLink(created.activation);
    } catch {
      // Shown in the form.
    }
  };

  return (
    <Modal isOpen={isOpen} onClose={close} size="lg">
      <ModalOverlay />
      <ModalContent>
        <ModalHeader>{link === null ? 'Invite user' : 'User invited'}</ModalHeader>
        <ModalCloseButton />
        {link !== null ? (
          <>
            <ModalBody>
              <ActivationLinkNotice link={link} />
            </ModalBody>
            <ModalFooter>
              <Button onClick={close}>Done</Button>
            </ModalFooter>
          </>
        ) : (
          <form onSubmit={(event) => void onSubmit(event)} noValidate>
            <ModalBody>
              <Stack spacing={4}>
                {apiError !== undefined && Object.keys(fields).length === 0 && (
                  <ApiErrorAlert
                    error={{ ...apiError, message: messageFor(apiError) }}
                    title="User not created"
                  />
                )}
                <FormControl isRequired isInvalid={fields.email !== undefined}>
                  <FormLabel>Email</FormLabel>
                  <Input
                    type="email"
                    value={email}
                    onChange={(event) => {
                      setEmail(event.target.value);
                    }}
                  />
                  <FormErrorMessage>{fields.email}</FormErrorMessage>
                </FormControl>
                <FormControl isRequired isInvalid={fields.display_name !== undefined}>
                  <FormLabel>Name</FormLabel>
                  <Input
                    value={name}
                    onChange={(event) => {
                      setName(event.target.value);
                    }}
                  />
                  <FormErrorMessage>{fields.display_name}</FormErrorMessage>
                </FormControl>
                {canAssign && roles.data !== undefined && (
                  <FormControl isInvalid={fields.roles !== undefined}>
                    <FormLabel>Roles</FormLabel>
                    <RoleCheckboxes roles={roles.data.items} value={selected} onChange={setSelected} />
                    <FormErrorMessage>{fields.roles}</FormErrorMessage>
                  </FormControl>
                )}
              </Stack>
            </ModalBody>
            <ModalFooter>
              <HStack>
                <Button variant="ghost" onClick={close}>
                  Cancel
                </Button>
                <Button type="submit" colorScheme="brand" isLoading={isLoading}>
                  Create and get link
                </Button>
              </HStack>
            </ModalFooter>
          </form>
        )}
      </ModalContent>
    </Modal>
  );
}

function UsersList() {
  const canManage = useHasPermission(PERMISSIONS.usersManage);
  const createModal = useDisclosure();
  const pager = useCursorPager();
  const [search, setSearch] = useState('');
  const [q, setQ] = useState('');
  const [status, setStatus] = useState<UserStatus | ''>('');
  const users = useListUsersQuery({
    q,
    ...(status !== '' && { status }),
    ...(pager.cursor !== undefined && { cursor: pager.cursor }),
    limit: PAGE_SIZE,
  });

  return (
    <Stack spacing={6}>
      <HStack justify="space-between">
        <Heading size="lg">Users</Heading>
        {canManage && (
          <Button colorScheme="brand" onClick={createModal.onOpen}>
            Invite user
          </Button>
        )}
      </HStack>
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
                  placeholder="Search by email or name"
                  aria-label="Search users"
                  value={search}
                  onChange={(event) => {
                    setSearch(event.target.value);
                  }}
                />
                <Select
                  aria-label="Status"
                  maxW="44"
                  value={status}
                  onChange={(event) => {
                    setStatus(event.target.value as UserStatus | '');
                    pager.reset();
                  }}
                >
                  <option value="">All statuses</option>
                  <option value="active">Active</option>
                  <option value="invited">Invited</option>
                  <option value="disabled">Disabled</option>
                </Select>
                <Button type="submit">Search</Button>
              </HStack>
            </form>
            {users.isLoading && <PageSpinner />}
            {users.isError && <ApiErrorAlert error={toApiError(users.error)} title="Cannot load users" />}
            {users.data !== undefined && (
              <>
                <TableContainer>
                  <Table size="sm">
                    <Thead>
                      <Tr>
                        <Th>Name</Th>
                        <Th>Email</Th>
                        <Th>Status</Th>
                        <Th>Roles</Th>
                      </Tr>
                    </Thead>
                    <Tbody>
                      {users.data.items.map((user) => (
                        <Tr key={user.id}>
                          <Td>
                            <Link as={RouterLink} to={`/admin/users/${user.id}`} color="brand.500">
                              {user.display_name}
                            </Link>
                          </Td>
                          <Td>{user.email}</Td>
                          <Td>
                            <StatusBadge status={user.status} />
                          </Td>
                          <Td>{user.roles.join(', ') || '—'}</Td>
                        </Tr>
                      ))}
                    </Tbody>
                  </Table>
                </TableContainer>
                {users.data.items.length === 0 && <Text color="gray.500">No users match.</Text>}
                <Pager pager={pager} nextCursor={users.data.next_cursor} />
              </>
            )}
          </Stack>
        </CardBody>
      </Card>
      {canManage && <CreateUserModal isOpen={createModal.isOpen} onClose={createModal.onClose} />}
    </Stack>
  );
}

export function UsersPage() {
  return (
    <RequirePermission permission={PERMISSIONS.usersRead}>
      <UsersList />
    </RequirePermission>
  );
}
