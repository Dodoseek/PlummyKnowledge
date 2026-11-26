import {
  Badge,
  Button,
  Card,
  CardBody,
  Heading,
  HStack,
  Select,
  Stack,
  Tab,
  TabList,
  Table,
  TableContainer,
  Tabs,
  Tbody,
  Td,
  Text,
  Th,
  Thead,
  Tr,
} from '@chakra-ui/react';
import { useState } from 'react';

import { toApiError } from '../../shared/api/errors';
import { messageFor } from '../../shared/api/messages';
import { ApiErrorAlert } from '../../shared/components/ApiErrorAlert';
import { PageSpinner } from '../../shared/components/PageSpinner';
import { ConfirmButton, Pager } from '../admin/components';
import { useCursorPager } from '../admin/useCursorPager';
import { PERMISSIONS, useHasPermission } from '../auth/permissions';
import { RequirePermission } from '../auth/RequirePermission';
import type { JobStatus } from '../sources/sourcesApi';
import {
  canCancel,
  canRetry,
  jobsApi,
  useCancelJobMutation,
  useListJobsQuery,
  useRetryJobMutation,
  type AnyJob,
  type JobType,
} from './jobsApi';

const PAGE_SIZE = 25;
const TYPES: JobType[] = ['ingestion', 'sync'];
const STATUSES: JobStatus[] = ['queued', 'running', 'succeeded', 'failed', 'dead', 'cancelled'];
const STATUS_COLORS: Record<JobStatus, string> = {
  queued: 'gray',
  running: 'blue',
  succeeded: 'green',
  failed: 'red',
  dead: 'red',
  cancelled: 'orange',
};
// Refresh while something is queued or running.
const POLL_INTERVAL_MS = 5000;

function formatTime(value: string | null): string {
  return value === null ? '—' : new Date(value).toLocaleString();
}

function JobActions({ job }: { job: AnyJob }) {
  const [retry, retrying] = useRetryJobMutation();
  const [cancel, cancelling] = useCancelJobMutation();
  const error = retrying.error ?? cancelling.error;
  return (
    <Stack spacing={1} align="flex-start">
      <HStack>
        {canRetry(job) && (
          <Button
            size="sm"
            variant="outline"
            isLoading={retrying.isLoading}
            onClick={() => {
              cancelling.reset();
              void retry({ type: job.type, id: job.id });
            }}
          >
            Retry
          </Button>
        )}
        {canCancel(job) && (
          <ConfirmButton
            label="Stop"
            confirmLabel="Stop job"
            isLoading={cancelling.isLoading}
            onConfirm={() => {
              retrying.reset();
              void cancel({ type: job.type, id: job.id });
            }}
          />
        )}
      </HStack>
      {error !== undefined && (
        <Text fontSize="xs" color="red.500" role="alert">
          {messageFor(toApiError(error))}
        </Text>
      )}
    </Stack>
  );
}

function subject(job: AnyJob): string {
  return job.type === 'sync'
    ? `Source ${job.data_source_id.slice(0, 8)} · ${job.mode}`
    : `Document ${job.document_id.slice(0, 8)} · ${job.kind}`;
}

function progress(job: AnyJob): string {
  if (job.type === 'sync') {
    const parts = Object.entries(job.stats)
      .filter(([, count]) => count > 0)
      .map(([name, count]) => `${name} ${String(count)}`);
    return parts.length > 0 ? parts.join(', ') : '—';
  }
  return job.result ?? job.stage;
}

function JobsList() {
  const canManage = useHasPermission(PERMISSIONS.jobsManage);
  const pager = useCursorPager();
  const [type, setType] = useState<JobType>('ingestion');
  const [status, setStatus] = useState<JobStatus | ''>('');
  const params = {
    type,
    ...(status !== '' && { status }),
    ...(pager.cursor !== undefined && { cursor: pager.cursor }),
    limit: PAGE_SIZE,
  };
  // Reads the cached page of the same parameters: poll only while something on it is queued or running.
  const cached = jobsApi.endpoints.listJobs.useQueryState(params);
  const active = cached.data?.items.some(canCancel) ?? false;
  const jobs = useListJobsQuery(params, { pollingInterval: active ? POLL_INTERVAL_MS : 0 });

  return (
    <Stack spacing={6}>
      <Heading size="lg">Jobs</Heading>
      <Card>
        <CardBody>
          <Stack spacing={4}>
            <HStack justify="space-between" flexWrap="wrap" rowGap={2}>
              <Tabs
                variant="soft-rounded"
                size="sm"
                index={TYPES.indexOf(type)}
                onChange={(index) => {
                  setType(TYPES[index] ?? 'ingestion');
                  pager.reset();
                }}
              >
                <TabList>
                  <Tab>Ingestion</Tab>
                  <Tab>Sync</Tab>
                </TabList>
              </Tabs>
              <Select
                aria-label="Status"
                maxW="44"
                value={status}
                onChange={(event) => {
                  setStatus(event.target.value as JobStatus | '');
                  pager.reset();
                }}
              >
                <option value="">All statuses</option>
                {STATUSES.map((value) => (
                  <option key={value} value={value}>
                    {value}
                  </option>
                ))}
              </Select>
            </HStack>
            {jobs.isLoading && <PageSpinner />}
            {jobs.isError && <ApiErrorAlert error={toApiError(jobs.error)} title="Cannot load jobs" />}
            {jobs.data !== undefined && (
              <>
                <TableContainer>
                  <Table size="sm">
                    <Thead>
                      <Tr>
                        <Th>Job</Th>
                        <Th>Status</Th>
                        <Th>Progress</Th>
                        <Th>Attempts</Th>
                        <Th>Error</Th>
                        <Th>Queued</Th>
                        <Th>Finished</Th>
                        {canManage && <Th>Actions</Th>}
                      </Tr>
                    </Thead>
                    <Tbody>
                      {jobs.data.items.map((job) => (
                        <Tr key={job.id} data-testid="job-row">
                          <Td>{subject(job)}</Td>
                          <Td>
                            <Badge colorScheme={STATUS_COLORS[job.status]}>{job.status}</Badge>
                          </Td>
                          <Td>{progress(job)}</Td>
                          <Td>
                            {job.attempt}/{job.max_attempts}
                          </Td>
                          <Td>
                            {job.error === null ? (
                              '—'
                            ) : (
                              <Text as="code" fontSize="xs">
                                {job.error.code}
                              </Text>
                            )}
                          </Td>
                          <Td>{formatTime(job.queued_at)}</Td>
                          <Td>{formatTime(job.finished_at)}</Td>
                          {canManage && (
                            <Td>
                              <JobActions job={job} />
                            </Td>
                          )}
                        </Tr>
                      ))}
                    </Tbody>
                  </Table>
                </TableContainer>
                {jobs.data.items.length === 0 && <Text color="gray.500">No jobs match.</Text>}
                <Pager pager={pager} nextCursor={jobs.data.next_cursor} />
              </>
            )}
          </Stack>
        </CardBody>
      </Card>
    </Stack>
  );
}

export function JobsPage() {
  return (
    <RequirePermission permission={PERMISSIONS.jobsRead}>
      <JobsList />
    </RequirePermission>
  );
}
