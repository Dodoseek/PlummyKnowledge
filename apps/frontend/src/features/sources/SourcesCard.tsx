import {
  Alert,
  AlertDescription,
  AlertIcon,
  Badge,
  Box,
  Button,
  Card,
  CardBody,
  CardHeader,
  Code,
  Divider,
  FormControl,
  FormLabel,
  Heading,
  HStack,
  Input,
  List,
  ListItem,
  Select,
  Stack,
  Text,
  Wrap,
} from '@chakra-ui/react';
import type { SerializedError } from '@reduxjs/toolkit';
import type { FetchBaseQueryError } from '@reduxjs/toolkit/query';
import { useEffect, useRef, useState, type FormEvent } from 'react';
import { useSearchParams } from 'react-router';

import { useAppDispatch } from '../../app/store/hooks';
import { baseApi } from '../../shared/api/baseApi';
import { toApiError } from '../../shared/api/errors';
import { ApiErrorAlert } from '../../shared/components/ApiErrorAlert';
import { PageSpinner } from '../../shared/components/PageSpinner';
import { ConfirmButton } from '../admin/components';
import { PERMISSIONS, useHasPermission } from '../auth/permissions';
import {
  formatTime,
  intervalLabel,
  INTERVALS,
  isWebUrl,
  browser,
  oauthFailureMessage,
  providerOf,
  PROVIDERS,
  sourceApiMessage,
  sourceErrorMessage,
  type ExternalType,
  type Provider,
} from './format';
import {
  isActiveJob,
  SYNC_POLL_INTERVAL_MS,
  syncedTags,
  useBrowseSourceQuery,
  useCreateSourceMutation,
  useDeleteSourceMutation,
  useDisconnectSourceMutation,
  useListSourcesQuery,
  useListSyncJobsQuery,
  useStartOAuthMutation,
  useSyncSourceMutation,
  useUpdateSourceMutation,
  useValidateSourceMutation,
  type BrowseEntry,
  type DataSource,
  type SourceStatus,
  type SyncJob,
  type Validation,
} from './sourcesApi';

type RequestError = FetchBaseQueryError | SerializedError | undefined;

function ErrorNotice({
  error,
  provider,
  title = 'Not done',
}: {
  error: RequestError;
  provider: Provider;
  title?: string;
}) {
  if (error === undefined) return null;
  const apiError = toApiError(error);
  return (
    <ApiErrorAlert error={{ ...apiError, message: sourceApiMessage(apiError, provider) }} title={title} />
  );
}

const ENTRY_PREFIXES: Record<BrowseEntry['kind'], string> = {
  folder: '',
  shared_drive: 'Shared drive: ',
  site: 'Site: ',
  drive: 'Library: ',
};

const STATUS_COLORS: Record<SourceStatus, string> = {
  active: 'green',
  paused: 'gray',
  error: 'red',
  disconnected: 'yellow',
};

const JOB_COLORS: Record<string, string> = {
  queued: 'gray',
  running: 'blue',
  succeeded: 'green',
  failed: 'red',
  dead: 'red',
  cancelled: 'yellow',
};

function OAuthNotice({ sources }: { sources: DataSource[] }) {
  const [params, setParams] = useSearchParams();
  // Read once: the parameters are removed from the address so a reload does not show the notice again.
  const [notice] = useState(() => ({
    oauth: params.get('oauth'),
    source: params.get('source'),
    reason: params.get('reason'),
  }));
  useEffect(() => {
    if (params.has('oauth') || params.has('source') || params.has('reason')) {
      const next = new URLSearchParams(params);
      ['oauth', 'source', 'reason'].forEach((key) => {
        next.delete(key);
      });
      setParams(next, { replace: true });
    }
  }, [params, setParams]);

  if (notice.oauth !== 'connected' && notice.oauth !== 'failed') return null;
  const source = sources.find((item) => item.id === notice.source);
  const name = source?.name;
  if (notice.oauth === 'connected') {
    return (
      <Alert status="success" borderRadius="md" data-testid="oauth-notice">
        <AlertIcon />
        <AlertDescription>
          {name !== undefined ? `“${name}” is connected.` : 'The source is connected.'} Check the granted
          access below and choose the folder to sync.
        </AlertDescription>
      </Alert>
    );
  }
  return (
    <Alert status="error" borderRadius="md" data-testid="oauth-notice">
      <AlertIcon />
      <AlertDescription>
        {oauthFailureMessage(notice.reason, source !== undefined ? providerOf(source.type) : undefined)}
      </AlertDescription>
    </Alert>
  );
}

function AddSourceForm({ kbId }: { kbId: string }) {
  const [createSource, state] = useCreateSourceMutation();
  const [name, setName] = useState('');
  const [type, setType] = useState<ExternalType>('google_drive');
  const onSubmit = async (event: FormEvent) => {
    event.preventDefault();
    try {
      await createSource({ kbId, name: name.trim(), type }).unwrap();
      setName('');
    } catch {
      // Shown from the mutation state.
    }
  };
  return (
    <form onSubmit={(event) => void onSubmit(event)} noValidate>
      <Stack spacing={3}>
        <HStack align="flex-end" wrap="wrap">
          <FormControl width="auto">
            <FormLabel>Provider</FormLabel>
            <Select
              value={type}
              onChange={(event) => {
                setType(event.target.value as ExternalType);
              }}
            >
              {Object.values(PROVIDERS).map((provider) => (
                <option key={provider.type} value={provider.type}>
                  {provider.service}
                </option>
              ))}
            </Select>
          </FormControl>
          <FormControl flex="1" minW="12rem">
            <FormLabel>Source name</FormLabel>
            <Input
              value={name}
              maxLength={200}
              placeholder="Name, e.g. Sales folder"
              onChange={(event) => {
                setName(event.target.value);
              }}
            />
          </FormControl>
          <Button type="submit" isLoading={state.isLoading} isDisabled={name.trim() === ''}>
            Add source
          </Button>
        </HStack>
        <ErrorNotice error={state.error} provider={PROVIDERS[type]} title="Source not added" />
      </Stack>
    </form>
  );
}

function FolderPage({
  source,
  parentId,
  cursor,
  isLast,
  onMore,
  onOpen,
  onChoose,
  isChoosing,
}: {
  source: DataSource;
  parentId: string | undefined;
  cursor: string | undefined;
  isLast: boolean;
  onMore: (cursor: string) => void;
  onOpen: (entry: BrowseEntry) => void;
  onChoose: (entry: BrowseEntry) => void;
  isChoosing: boolean;
}) {
  const provider = providerOf(source.type);
  const page = useBrowseSourceQuery(
    {
      id: source.id,
      ...(parentId !== undefined ? { parentId } : {}),
      ...(cursor !== undefined ? { cursor } : {}),
    },
    { refetchOnMountOrArgChange: true },
  );
  if (page.isFetching && page.data === undefined) return <PageSpinner />;
  if (page.isError) return <ErrorNotice error={page.error} provider={provider} title="Cannot list folders" />;
  const items = page.data?.items ?? [];
  const next = page.data?.next_cursor ?? null;
  return (
    <>
      {cursor === undefined && items.length === 0 && (
        <Text color="gray.500" fontSize="sm">
          No folders here.
        </Text>
      )}
      <List spacing={1}>
        {items.map((entry) => (
          <ListItem key={entry.id}>
            <HStack justify="space-between">
              <Button
                size="sm"
                variant="ghost"
                onClick={() => {
                  onOpen(entry);
                }}
              >
                {ENTRY_PREFIXES[entry.kind]}
                {entry.name}
              </Button>
              {entry.kind !== 'site' && (
                <Button
                  size="xs"
                  variant="outline"
                  aria-label={`Choose ${entry.name}`}
                  isLoading={isChoosing}
                  onClick={() => {
                    onChoose(entry);
                  }}
                >
                  Choose
                </Button>
              )}
            </HStack>
          </ListItem>
        ))}
      </List>
      {isLast && next !== null && (
        <Button
          size="sm"
          variant="outline"
          alignSelf="flex-start"
          onClick={() => {
            onMore(next);
          }}
        >
          More
        </Button>
      )}
    </>
  );
}

function FolderPicker({ source, onChosen }: { source: DataSource; onChosen: () => void }) {
  const [updateSource, updateState] = useUpdateSourceMutation();
  const provider = providerOf(source.type);
  // The folders opened so far and the extra pages of the open one ("More"): changing folder resets the pages.
  const [path, setPath] = useState<BrowseEntry[]>([]);
  const [cursors, setCursors] = useState<string[]>([]);
  const parent = path.at(-1);
  const goTo = (next: BrowseEntry[]) => {
    setPath(next);
    setCursors([]);
  };

  const choose = async (entry: BrowseEntry) => {
    try {
      await updateSource({ kbId: source.knowledge_base_id, id: source.id, folder_id: entry.id }).unwrap();
      onChosen();
    } catch {
      // Shown from the mutation state.
    }
  };
  const pages: (string | undefined)[] = [undefined, ...cursors];

  return (
    <Box borderWidth="1px" borderRadius="md" p={3} data-testid="folder-picker">
      <Stack spacing={3}>
        <Wrap spacing={1} fontSize="sm" align="center">
          <Button
            size="xs"
            variant="link"
            onClick={() => {
              goTo([]);
            }}
          >
            {provider.root}
          </Button>
          {path.map((entry, index) => (
            <HStack key={entry.id} spacing={1}>
              <Text>/</Text>
              <Button
                size="xs"
                variant="link"
                onClick={() => {
                  goTo(path.slice(0, index + 1));
                }}
              >
                {entry.name}
              </Button>
            </HStack>
          ))}
        </Wrap>
        {parent !== undefined && parent.kind !== 'site' && (
          <Button
            size="sm"
            colorScheme="brand"
            alignSelf="flex-start"
            isLoading={updateState.isLoading}
            onClick={() => void choose(parent)}
          >
            Sync “{parent.name}”
          </Button>
        )}
        <ErrorNotice error={updateState.error} provider={provider} title="Folder not saved" />
        {pages.map((cursor, index) => (
          <FolderPage
            key={`${parent?.id ?? 'root'}:${cursor ?? ''}`}
            source={source}
            parentId={parent?.id}
            cursor={cursor}
            isLast={index === pages.length - 1}
            onMore={(next) => {
              setCursors((list) => [...list, next]);
            }}
            onOpen={(entry) => {
              goTo([...path, entry]);
            }}
            onChoose={(entry) => void choose(entry)}
            isChoosing={updateState.isLoading}
          />
        ))}
      </Stack>
    </Box>
  );
}

function JobLine({ job, provider }: { job: SyncJob; provider: Provider }) {
  const counts = Object.entries(job.stats).filter(([, value]) => value > 0);
  return (
    <HStack spacing={2} fontSize="sm" wrap="wrap" data-testid="latest-sync-job">
      <Text>Latest sync ({job.mode}):</Text>
      <Badge colorScheme={JOB_COLORS[job.status] ?? 'gray'}>{job.status}</Badge>
      {counts.length > 0 && (
        <Text color="gray.600">{counts.map(([key, value]) => `${String(value)} ${key}`).join(', ')}</Text>
      )}
      {job.error !== null && <Text color="red.600">{sourceErrorMessage(job.error.code, provider)}</Text>}
    </HStack>
  );
}

/** The latest sync job, refreshed while it is queued or running; when it ends, the documents reload. */
function useLatestJob(source: DataSource, enabled: boolean) {
  const dispatch = useAppDispatch();
  const [active, setActive] = useState(false);
  const jobs = useListSyncJobsQuery(source.id, {
    skip: !enabled,
    pollingInterval: active ? SYNC_POLL_INTERVAL_MS : 0,
    skipPollingIfUnfocused: true,
  });
  const job = jobs.data?.items[0];
  const nowActive = job !== undefined && isActiveJob(job.status);
  const wasActive = useRef(false);
  useEffect(() => {
    if (wasActive.current && !nowActive) {
      dispatch(baseApi.util.invalidateTags([...syncedTags(source.knowledge_base_id)]));
    }
    wasActive.current = nowActive;
  }, [dispatch, nowActive, source.knowledge_base_id]);
  if (nowActive !== active) setActive(nowActive);
  const refetch = () => {
    if (enabled) void jobs.refetch();
  };
  return { job, refetch };
}

function ValidationNotice({ result, provider }: { result: Validation; provider: Provider }) {
  const ok = result.status === 'ok';
  return (
    <Alert status={ok ? 'success' : 'error'} borderRadius="md" data-testid="validation-result">
      <AlertIcon />
      <AlertDescription>
        {ok
          ? `Connection works${result.folder_accessible === false ? ', but the folder is not accessible' : ''}.`
          : `Connection check failed: ${sourceErrorMessage(result.error_code ?? 'connector_error', provider)}`}
      </AlertDescription>
    </Alert>
  );
}

function SourceItem({
  source,
  canManage,
  canSync,
  canReadJobs,
}: {
  source: DataSource;
  canManage: boolean;
  canSync: boolean;
  canReadJobs: boolean;
}) {
  const kbId = source.knowledge_base_id;
  const provider = providerOf(source.type);
  const [startOAuth, oauthState] = useStartOAuthMutation();
  const [syncSource, syncState] = useSyncSourceMutation();
  const [validateSource, validateState] = useValidateSourceMutation();
  const [updateSource, updateState] = useUpdateSourceMutation();
  const [disconnectSource, disconnectState] = useDisconnectSourceMutation();
  const [deleteSource, deleteState] = useDeleteSourceMutation();
  const [picking, setPicking] = useState(false);
  const { job, refetch } = useLatestJob(source, canReadJobs);
  const credentials = source.credentials;
  const connected = credentials !== null && credentials.status === 'valid';
  const syncing = job !== undefined && isActiveJob(job.status);

  const connect = async () => {
    try {
      const { authorization_url: url } = await startOAuth(source.id).unwrap();
      if (isWebUrl(url)) browser.assign(url);
    } catch {
      // Shown from the mutation state.
    }
  };
  const sync = async (mode: 'incremental' | 'reconciliation') => {
    try {
      await syncSource({ kbId, id: source.id, mode }).unwrap();
      refetch();
    } catch {
      // Shown from the mutation state.
    }
  };

  return (
    <Stack spacing={3} data-testid={`source-${source.id}`}>
      <HStack spacing={3} wrap="wrap">
        <Heading size="sm">{source.name}</Heading>
        <Badge>{provider.service}</Badge>
        <Badge colorScheme={STATUS_COLORS[source.status]}>{source.status}</Badge>
      </HStack>

      {credentials === null ? (
        <Text fontSize="sm" color="gray.600">
          Not connected to a {provider.account}.
        </Text>
      ) : (
        <Stack spacing={2} fontSize="sm">
          <Text>
            Account: {credentials.account_label ?? 'unknown'}{' '}
            {credentials.status !== 'valid' && <Badge colorScheme="red">{credentials.status}</Badge>}
          </Text>
          <Box>
            <Text>Access granted by {provider.company}:</Text>
            <List data-testid="granted-scopes">
              {credentials.granted_scopes.map((scope) => (
                <ListItem key={scope}>
                  <Code fontSize="xs" wordBreak="break-all">
                    {scope}
                  </Code>
                </ListItem>
              ))}
            </List>
          </Box>
          <Alert status="warning" borderRadius="md" fontSize="sm">
            <AlertIcon />
            <AlertDescription>
              Choosing a folder does not limit this access: the server can read {provider.reach}. Connect an
              account that sees only the folders this knowledge base needs.
            </AlertDescription>
          </Alert>
        </Stack>
      )}

      <Stack spacing={1} fontSize="sm">
        <Text>
          Folder: {source.folder !== null ? (source.folder.name ?? source.folder.id) : 'not chosen'}
        </Text>
        {source.sync !== null && (
          <>
            <Text>Schedule: {intervalLabel(source.sync.interval_minutes)}</Text>
            <Text>
              Last sync: {formatTime(source.sync.last_sync_at)}
              {source.sync.last_status !== null && ` (${source.sync.last_status})`}
            </Text>
            {source.sync.next_sync_at !== null && (
              <Text>Next sync: {formatTime(source.sync.next_sync_at)}</Text>
            )}
            {source.sync.last_error !== null && (
              <Text color="red.600" data-testid="last-sync-error">
                {sourceErrorMessage(source.sync.last_error, provider)}
              </Text>
            )}
          </>
        )}
        {job !== undefined && <JobLine job={job} provider={provider} />}
      </Stack>

      <Wrap spacing={2}>
        {canManage && (
          <Button size="sm" variant="outline" isLoading={oauthState.isLoading} onClick={() => void connect()}>
            {credentials === null ? `Connect ${provider.account}` : 'Reconnect'}
          </Button>
        )}
        {canManage && connected && (
          <Button
            size="sm"
            variant="outline"
            onClick={() => {
              setPicking((value) => !value);
            }}
          >
            {picking ? 'Close folder list' : source.folder === null ? 'Choose folder' : 'Change folder'}
          </Button>
        )}
        {canSync && source.status === 'active' && (
          <>
            <Button
              size="sm"
              colorScheme="brand"
              isLoading={syncState.isLoading}
              isDisabled={syncing}
              onClick={() => void sync('incremental')}
            >
              Sync now
            </Button>
            <Button
              size="sm"
              variant="outline"
              isLoading={syncState.isLoading}
              isDisabled={syncing}
              onClick={() => void sync('reconciliation')}
            >
              Full check
            </Button>
          </>
        )}
        {canManage && credentials !== null && (
          <Button
            size="sm"
            variant="outline"
            isLoading={validateState.isLoading}
            onClick={() => void validateSource(source.id)}
          >
            Check connection
          </Button>
        )}
        {canManage && (
          <Select
            size="sm"
            width="auto"
            aria-label="Sync schedule"
            value={source.sync?.interval_minutes != null ? String(source.sync.interval_minutes) : ''}
            isDisabled={updateState.isLoading}
            onChange={(event) => {
              const value = event.target.value;
              void updateSource({
                kbId,
                id: source.id,
                sync_interval_minutes: value === '' ? null : Number(value),
              });
            }}
          >
            {INTERVALS.map((item) => (
              <option key={item.value} value={item.value}>
                {item.label}
              </option>
            ))}
          </Select>
        )}
        {canManage && credentials !== null && (
          <ConfirmButton
            label="Disconnect"
            confirmLabel="Disconnect account"
            isLoading={disconnectState.isLoading}
            onConfirm={() => void disconnectSource({ kbId, id: source.id })}
          />
        )}
        {canManage && (
          <ConfirmButton
            label="Delete source"
            confirmLabel="Delete source and its documents"
            isLoading={deleteState.isLoading}
            onConfirm={() => void deleteSource({ kbId, id: source.id })}
          />
        )}
      </Wrap>

      {picking && (
        <FolderPicker
          source={source}
          onChosen={() => {
            setPicking(false);
            refetch();
          }}
        />
      )}
      {validateState.data !== undefined && (
        <ValidationNotice result={validateState.data} provider={provider} />
      )}
      {disconnectState.isSuccess && provider.type === 'microsoft_365' && (
        <Alert status="info" borderRadius="md" fontSize="sm" data-testid="revoke-hint">
          <AlertIcon />
          <AlertDescription>
            The stored token was deleted. Microsoft has no way to revoke a single token, so the consent stays
            until it is removed: in the account’s My Apps page, or by an Entra administrator for the whole
            organization.
          </AlertDescription>
        </Alert>
      )}
      <ErrorNotice error={oauthState.error} provider={provider} />
      <ErrorNotice error={syncState.error} provider={provider} title="Sync not started" />
      <ErrorNotice error={validateState.error} provider={provider} />
      <ErrorNotice error={updateState.error} provider={provider} />
      <ErrorNotice error={disconnectState.error} provider={provider} />
      <ErrorNotice error={deleteState.error} provider={provider} />
    </Stack>
  );
}

/**
 * External sources of a knowledge base (ADR-020, ADR-021). Controls are shown by permission and KB access only for
 * convenience: the backend checks both on every request.
 */
export function SourcesCard({ kbId, canManageKb }: { kbId: string; canManageKb: boolean }) {
  const canManage = useHasPermission(PERMISSIONS.dataSourcesManage) && canManageKb;
  const canSync = useHasPermission(PERMISSIONS.dataSourcesSync) && canManageKb;
  const canReadJobs = useHasPermission(PERMISSIONS.jobsRead);
  const sources = useListSourcesQuery({ kbId });
  const external = (sources.data?.items ?? []).filter((source) => source.type !== 'upload');

  return (
    <Card>
      <CardHeader pb={0}>
        <Heading size="md">External sources</Heading>
      </CardHeader>
      <CardBody>
        <Stack spacing={4}>
          {/* After the list: the notice names the source and its provider. */}
          {!sources.isLoading && <OAuthNotice sources={external} />}
          {sources.isLoading && <PageSpinner />}
          {sources.isError && (
            <ErrorNotice
              error={sources.error}
              provider={PROVIDERS.google_drive}
              title="Cannot load sources"
            />
          )}
          {sources.data !== undefined && external.length === 0 && (
            <Text color="gray.500">No external sources yet.</Text>
          )}
          {external.map((source, index) => (
            <Stack key={source.id} spacing={4}>
              {index > 0 && <Divider />}
              <SourceItem source={source} canManage={canManage} canSync={canSync} canReadJobs={canReadJobs} />
            </Stack>
          ))}
          {canManage && (
            <>
              <Divider />
              <AddSourceForm kbId={kbId} />
            </>
          )}
        </Stack>
      </CardBody>
    </Card>
  );
}
