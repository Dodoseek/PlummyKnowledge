import {
  Badge,
  Button,
  Card,
  CardBody,
  CardHeader,
  FormControl,
  FormErrorMessage,
  FormLabel,
  Heading,
  Input,
  Stack,
  Table,
  TableContainer,
  Tbody,
  Td,
  Text,
  Th,
  Thead,
  Tr,
  useToast,
} from '@chakra-ui/react';
import { useState, type FormEvent } from 'react';
import { useNavigate } from 'react-router';

import { baseApi } from '../../shared/api/baseApi';
import { toApiError } from '../../shared/api/errors';
import { fieldErrorsByName, messageFor } from '../../shared/api/messages';
import { csrfTokenCleared } from '../../shared/auth/csrfSlice';
import { useAppDispatch } from '../../app/store/hooks';
import { ApiErrorAlert } from '../../shared/components/ApiErrorAlert';
import { PageSpinner } from '../../shared/components/PageSpinner';
import {
  authApi,
  useChangePasswordMutation,
  useListMySessionsQuery,
  useRevokeMySessionMutation,
  type SessionItem,
} from '../auth/authApi';

const formatDate = (value: string) => new Date(value).toLocaleString();

function ChangePasswordCard() {
  const toast = useToast();
  const [changePassword, { isLoading, error, reset }] = useChangePasswordMutation();
  const [current, setCurrent] = useState('');
  const [next, setNext] = useState('');
  const [confirmation, setConfirmation] = useState('');
  const [submitted, setSubmitted] = useState(false);

  const apiError = error === undefined ? undefined : toApiError(error);
  const fields = fieldErrorsByName(apiError);
  const mismatch = submitted && next !== confirmation;

  const onSubmit = async (event: FormEvent) => {
    event.preventDefault();
    setSubmitted(true);
    if (current === '' || next === '' || next !== confirmation) return;
    try {
      await changePassword({ current_password: current, new_password: next }).unwrap();
      setCurrent('');
      setNext('');
      setConfirmation('');
      setSubmitted(false);
      reset();
      toast({ status: 'success', title: 'Password changed. Other sessions were signed out.' });
    } catch {
      setCurrent('');
    }
  };

  return (
    <Card>
      <CardHeader pb={0}>
        <Heading size="md">Change password</Heading>
      </CardHeader>
      <CardBody>
        <form onSubmit={(event) => void onSubmit(event)} noValidate>
          <Stack spacing={4} maxW="md">
            {apiError !== undefined && Object.keys(fields).length === 0 && (
              <ApiErrorAlert
                error={{ ...apiError, message: messageFor(apiError) }}
                title="Password not changed"
              />
            )}
            <FormControl
              isRequired
              isInvalid={(submitted && current === '') || fields.current_password !== undefined}
            >
              <FormLabel>Current password</FormLabel>
              <Input
                type="password"
                autoComplete="current-password"
                value={current}
                onChange={(event) => {
                  setCurrent(event.target.value);
                }}
              />
              <FormErrorMessage>{fields.current_password ?? 'Enter your current password.'}</FormErrorMessage>
            </FormControl>
            <FormControl
              isRequired
              isInvalid={(submitted && next === '') || fields.new_password !== undefined}
            >
              <FormLabel>New password</FormLabel>
              <Input
                type="password"
                autoComplete="new-password"
                value={next}
                onChange={(event) => {
                  setNext(event.target.value);
                }}
              />
              <FormErrorMessage>{fields.new_password ?? 'Choose a new password.'}</FormErrorMessage>
            </FormControl>
            <FormControl isRequired isInvalid={mismatch}>
              <FormLabel>Repeat new password</FormLabel>
              <Input
                type="password"
                autoComplete="new-password"
                value={confirmation}
                onChange={(event) => {
                  setConfirmation(event.target.value);
                }}
              />
              <FormErrorMessage>The passwords do not match.</FormErrorMessage>
            </FormControl>
            <Button type="submit" colorScheme="brand" alignSelf="flex-start" isLoading={isLoading}>
              Change password
            </Button>
          </Stack>
        </form>
      </CardBody>
    </Card>
  );
}

function SessionsCard() {
  const navigate = useNavigate();
  const dispatch = useAppDispatch();
  const sessions = useListMySessionsQuery();
  const [revoke, revoking] = useRevokeMySessionMutation();

  const onRevoke = async (session: SessionItem) => {
    try {
      await revoke(session.id).unwrap();
    } catch {
      return;
    }
    if (session.current) {
      dispatch(csrfTokenCleared());
      dispatch(baseApi.util.resetApiState());
      void navigate('/login', { replace: true });
    }
  };

  return (
    <Card>
      <CardHeader pb={0}>
        <Heading size="md">Active sessions</Heading>
      </CardHeader>
      <CardBody>
        {sessions.isLoading && <PageSpinner />}
        {sessions.isError && (
          <ApiErrorAlert error={toApiError(sessions.error)} title="Cannot load sessions" />
        )}
        {revoking.error !== undefined && (
          <ApiErrorAlert error={toApiError(revoking.error)} title="Cannot end the session" />
        )}
        {sessions.data !== undefined && (
          <TableContainer>
            <Table size="sm">
              <Thead>
                <Tr>
                  <Th>Device</Th>
                  <Th>IP address</Th>
                  <Th>Signed in</Th>
                  <Th>Last active</Th>
                  <Th />
                </Tr>
              </Thead>
              <Tbody>
                {sessions.data.items.map((session) => (
                  <Tr key={session.id} data-testid="session-row">
                    <Td maxW="xs" whiteSpace="normal">
                      <Text noOfLines={2}>{session.user_agent ?? 'Unknown device'}</Text>
                      {session.current && <Badge colorScheme="green">This browser</Badge>}
                    </Td>
                    <Td>{session.ip_address ?? '—'}</Td>
                    <Td>{formatDate(session.created_at)}</Td>
                    <Td>{formatDate(session.last_seen_at)}</Td>
                    <Td textAlign="right">
                      <Button
                        size="xs"
                        variant="outline"
                        colorScheme="red"
                        onClick={() => void onRevoke(session)}
                        isLoading={revoking.isLoading && revoking.originalArgs === session.id}
                      >
                        {session.current ? 'Sign out' : 'End session'}
                      </Button>
                    </Td>
                  </Tr>
                ))}
              </Tbody>
            </Table>
          </TableContainer>
        )}
      </CardBody>
    </Card>
  );
}

export function AccountPage() {
  const { data: session } = authApi.endpoints.getSession.useQueryState();
  return (
    <Stack spacing={6}>
      <Stack spacing={1}>
        <Heading size="lg">My account</Heading>
        {session !== undefined && (
          <Text color="gray.500">
            {session.user.display_name} · {session.user.email}
          </Text>
        )}
      </Stack>
      <ChangePasswordCard />
      <SessionsCard />
    </Stack>
  );
}
