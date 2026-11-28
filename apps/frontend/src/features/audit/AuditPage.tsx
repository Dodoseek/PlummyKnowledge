import {
  Badge,
  Card,
  CardBody,
  FormControl,
  FormLabel,
  Heading,
  HStack,
  Input,
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
} from '@chakra-ui/react';
import { useState } from 'react';

import { toApiError } from '../../shared/api/errors';
import { ApiErrorAlert } from '../../shared/components/ApiErrorAlert';
import { PageSpinner } from '../../shared/components/PageSpinner';
import { Pager } from '../admin/components';
import { useCursorPager } from '../admin/useCursorPager';
import { PERMISSIONS } from '../auth/permissions';
import { RequirePermission } from '../auth/RequirePermission';
import {
  toInstant,
  useListAuditEventsQuery,
  type AuditEvent,
  type AuditListParams,
  type AuditOutcome,
} from './auditApi';

const PAGE_SIZE = 50;
const OUTCOMES: AuditOutcome[] = ['success', 'denied', 'failure'];
const OUTCOME_COLORS: Record<AuditOutcome, string> = { success: 'green', denied: 'orange', failure: 'red' };
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

function actor(event: AuditEvent): string {
  return event.actor_user_id === null ? event.actor_type : `user ${event.actor_user_id.slice(0, 8)}`;
}

function target(event: AuditEvent): string {
  if (event.target_type === null) return '—';
  return event.target_id === null ? event.target_type : `${event.target_type} ${event.target_id.slice(0, 8)}`;
}

function details(event: AuditEvent): string {
  const parts = Object.entries(event.details).map(([key, value]) => `${key}=${String(value)}`);
  return parts.length > 0 ? parts.join(', ') : '—';
}

function AuditList() {
  const pager = useCursorPager();
  const [action, setAction] = useState('');
  const [actorId, setActorId] = useState('');
  const [outcome, setOutcome] = useState<AuditOutcome | ''>('');
  const [from, setFrom] = useState('');
  const [to, setTo] = useState('');
  const trimmedActor = actorId.trim();
  const fromInstant = toInstant(from);
  const toInstantValue = toInstant(to);
  const params: AuditListParams = {
    ...(action.trim() !== '' && { action: action.trim() }),
    ...(UUID.test(trimmedActor) && { actor_user_id: trimmedActor }),
    ...(outcome !== '' && { outcome }),
    ...(fromInstant !== undefined && { from: fromInstant }),
    ...(toInstantValue !== undefined && { to: toInstantValue }),
    ...(pager.cursor !== undefined && { cursor: pager.cursor }),
    limit: PAGE_SIZE,
  };
  const events = useListAuditEventsQuery(params);
  const filter =
    <T,>(set: (value: T) => void) =>
    (value: T) => {
      set(value);
      pager.reset();
    };

  return (
    <Stack spacing={6}>
      <Heading size="lg">Audit log</Heading>
      <Card>
        <CardBody>
          <Stack spacing={4}>
            <HStack align="flex-end" flexWrap="wrap" rowGap={2}>
              <FormControl maxW="56">
                <FormLabel fontSize="sm">Action</FormLabel>
                <Input
                  size="sm"
                  placeholder="auth.login.failed"
                  value={action}
                  onChange={(event) => {
                    filter(setAction)(event.target.value);
                  }}
                />
              </FormControl>
              <FormControl maxW="72" isInvalid={trimmedActor !== '' && !UUID.test(trimmedActor)}>
                <FormLabel fontSize="sm">Actor user ID</FormLabel>
                <Input
                  size="sm"
                  value={actorId}
                  onChange={(event) => {
                    filter(setActorId)(event.target.value);
                  }}
                />
              </FormControl>
              <FormControl maxW="40">
                <FormLabel fontSize="sm">Outcome</FormLabel>
                <Select
                  size="sm"
                  value={outcome}
                  onChange={(event) => {
                    filter(setOutcome)(event.target.value as AuditOutcome | '');
                  }}
                >
                  <option value="">All outcomes</option>
                  {OUTCOMES.map((value) => (
                    <option key={value} value={value}>
                      {value}
                    </option>
                  ))}
                </Select>
              </FormControl>
              <FormControl maxW="56">
                <FormLabel fontSize="sm">From</FormLabel>
                <Input
                  size="sm"
                  type="datetime-local"
                  value={from}
                  onChange={(event) => {
                    filter(setFrom)(event.target.value);
                  }}
                />
              </FormControl>
              <FormControl maxW="56">
                <FormLabel fontSize="sm">To</FormLabel>
                <Input
                  size="sm"
                  type="datetime-local"
                  value={to}
                  onChange={(event) => {
                    filter(setTo)(event.target.value);
                  }}
                />
              </FormControl>
            </HStack>
            {events.isLoading && <PageSpinner />}
            {events.isError && (
              <ApiErrorAlert error={toApiError(events.error)} title="Cannot load the audit log" />
            )}
            {events.data !== undefined && (
              <>
                <TableContainer>
                  <Table size="sm">
                    <Thead>
                      <Tr>
                        <Th>Time</Th>
                        <Th>Action</Th>
                        <Th>Outcome</Th>
                        <Th>Actor</Th>
                        <Th>Target</Th>
                        <Th>Details</Th>
                        <Th>IP address</Th>
                      </Tr>
                    </Thead>
                    <Tbody>
                      {events.data.items.map((event) => (
                        <Tr key={event.id} data-testid="audit-row">
                          <Td>{new Date(event.created_at).toLocaleString()}</Td>
                          <Td>
                            <Text as="code" fontSize="xs">
                              {event.action}
                            </Text>
                          </Td>
                          <Td>
                            <Badge colorScheme={OUTCOME_COLORS[event.outcome]}>{event.outcome}</Badge>
                          </Td>
                          <Td>{actor(event)}</Td>
                          <Td>{target(event)}</Td>
                          <Td whiteSpace="normal" fontSize="xs">
                            {details(event)}
                          </Td>
                          <Td>{event.ip_address ?? '—'}</Td>
                        </Tr>
                      ))}
                    </Tbody>
                  </Table>
                </TableContainer>
                {events.data.items.length === 0 && <Text color="gray.500">No events match.</Text>}
                <Pager pager={pager} nextCursor={events.data.next_cursor} />
              </>
            )}
          </Stack>
        </CardBody>
      </Card>
    </Stack>
  );
}

export function AuditPage() {
  return (
    <RequirePermission permission={PERMISSIONS.auditRead}>
      <AuditList />
    </RequirePermission>
  );
}
