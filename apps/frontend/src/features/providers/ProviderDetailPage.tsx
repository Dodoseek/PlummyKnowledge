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
  Input,
  Link,
  NumberInput,
  NumberInputField,
  SimpleGrid,
  Stack,
  Switch,
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
import { Link as RouterLink, useNavigate, useParams } from 'react-router';

import { toApiError, type ApiError } from '../../shared/api/errors';
import { fieldErrorsByName, messageFor } from '../../shared/api/messages';
import { ApiErrorAlert } from '../../shared/components/ApiErrorAlert';
import { PageSpinner } from '../../shared/components/PageSpinner';
import { ConfirmButton } from '../admin/components';
import { PERMISSIONS, useHasPermission } from '../auth/permissions';
import { RequirePermission } from '../auth/RequirePermission';
import { EnabledBadge, KeySummary, LastTestBadge, TestOutcome } from './components';
import {
  PROVIDER_TYPES,
  providerTypeLabel,
  useCreateModelMutation,
  useDeleteModelMutation,
  useDeleteProviderMutation,
  useGetProviderQuery,
  useListModelsQuery,
  useTestProviderMutation,
  useUpdateModelMutation,
  useUpdateProviderMutation,
  type ConnectionTestResult,
  type Provider,
} from './providersApi';

function DetailsCard({ provider, canManage }: { provider: Provider; canManage: boolean }) {
  const toast = useToast();
  const [updateProvider, { isLoading, error }] = useUpdateProviderMutation();
  const [name, setName] = useState(provider.name);
  const [baseUrl, setBaseUrl] = useState(provider.base_url);
  const [timeout, setTimeoutSeconds] = useState(String(provider.settings.timeout_seconds));
  const apiError = error === undefined ? undefined : toApiError(error);
  const fields = fieldErrorsByName(apiError);

  const onSave = async (event: FormEvent) => {
    event.preventDefault();
    try {
      await updateProvider({
        id: provider.id,
        name: name.trim(),
        base_url: baseUrl.trim(),
        settings: { timeout_seconds: Number(timeout) },
      }).unwrap();
      toast({ status: 'success', title: 'Provider saved' });
    } catch {
      // Shown below.
    }
  };

  return (
    <Card>
      <CardHeader pb={0}>
        <Heading size="md">Settings</Heading>
      </CardHeader>
      <CardBody>
        <form onSubmit={(event) => void onSave(event)} noValidate>
          <Stack spacing={4}>
            <SimpleGrid columns={{ base: 1, md: 2 }} spacing={4}>
              <FormControl isInvalid={fields.name !== undefined} isReadOnly={!canManage}>
                <FormLabel>Name</FormLabel>
                <Input
                  value={name}
                  onChange={(event) => {
                    setName(event.target.value);
                  }}
                />
                <FormErrorMessage>{fields.name}</FormErrorMessage>
              </FormControl>
              <FormControl isInvalid={fields.timeout_seconds !== undefined} isReadOnly={!canManage}>
                <FormLabel>Timeout, seconds</FormLabel>
                <NumberInput
                  min={1}
                  max={600}
                  value={timeout}
                  onChange={(value) => {
                    setTimeoutSeconds(value);
                  }}
                >
                  <NumberInputField />
                </NumberInput>
                <FormErrorMessage>{fields.timeout_seconds}</FormErrorMessage>
              </FormControl>
              <FormControl
                isInvalid={fields.base_url !== undefined}
                isReadOnly={!canManage}
                gridColumn="1 / -1"
              >
                <FormLabel>Base URL</FormLabel>
                <Input
                  value={baseUrl}
                  onChange={(event) => {
                    setBaseUrl(event.target.value);
                  }}
                />
                <FormErrorMessage>
                  {apiError?.code === 'endpoint_not_allowed' ? messageFor(apiError) : fields.base_url}
                </FormErrorMessage>
              </FormControl>
            </SimpleGrid>
            {apiError !== undefined && Object.keys(fields).length === 0 && (
              <ApiErrorAlert error={{ ...apiError, message: messageFor(apiError) }} title="Not saved" />
            )}
            {canManage && (
              <HStack>
                <Button type="submit" colorScheme="brand" isLoading={isLoading}>
                  Save
                </Button>
                <Switch
                  isChecked={provider.enabled}
                  onChange={(event) => {
                    void updateProvider({ id: provider.id, enabled: event.target.checked });
                  }}
                >
                  Enabled
                </Switch>
              </HStack>
            )}
          </Stack>
        </form>
      </CardBody>
    </Card>
  );
}

function KeyCard({ provider }: { provider: Provider }) {
  const toast = useToast();
  const [updateProvider, { isLoading, reset }] = useUpdateProviderMutation();
  // Write-only: kept in this component only and cleared after saving.
  const [apiKey, setApiKey] = useState('');
  const [error, setError] = useState<ApiError>();
  const keyRequired = PROVIDER_TYPES.find((item) => item.value === provider.provider_type)?.keyRequired;

  const save = async (value: string | null) => {
    setError(undefined);
    try {
      await updateProvider({ id: provider.id, api_key: value }).unwrap();
      setApiKey('');
      toast({ status: 'success', title: value === null ? 'Key removed' : 'Key replaced' });
    } catch (caught) {
      setError(toApiError(caught as Parameters<typeof toApiError>[0]));
    } finally {
      reset(); // the request carried the key: drop it from the RTK Query cache
    }
  };

  return (
    <Card>
      <CardHeader pb={0}>
        <Heading size="md">API key</Heading>
      </CardHeader>
      <CardBody>
        <Stack spacing={4}>
          <HStack>
            <Text>Current key:</Text>
            <KeySummary provider={provider} />
          </HStack>
          <form
            autoComplete="off"
            onSubmit={(event) => {
              event.preventDefault();
              if (apiKey !== '') void save(apiKey);
            }}
          >
            <HStack align="flex-start">
              <FormControl isInvalid={(error?.fieldErrors ?? []).length > 0}>
                <Input
                  type="password"
                  aria-label="New API key"
                  placeholder={provider.has_secret ? 'Enter a new key to replace it' : 'Enter a key'}
                  autoComplete="off"
                  spellCheck={false}
                  value={apiKey}
                  onChange={(event) => {
                    setApiKey(event.target.value);
                  }}
                />
                <FormErrorMessage>{error?.fieldErrors?.[0]?.message}</FormErrorMessage>
              </FormControl>
              <Button type="submit" isLoading={isLoading} isDisabled={apiKey === ''} flexShrink={0}>
                {provider.has_secret ? 'Replace key' : 'Set key'}
              </Button>
            </HStack>
          </form>
          {provider.has_secret && !keyRequired && (
            <ConfirmButton
              label="Remove key"
              confirmLabel="Remove the key"
              onConfirm={() => void save(null)}
              isLoading={isLoading}
            />
          )}
          {error !== undefined && (error.fieldErrors ?? []).length === 0 && (
            <ApiErrorAlert error={{ ...error, message: messageFor(error) }} title="Key not changed" />
          )}
        </Stack>
      </CardBody>
    </Card>
  );
}

function ConnectionCard({
  provider,
  canManage,
  onResult,
}: {
  provider: Provider;
  canManage: boolean;
  onResult: (result: ConnectionTestResult) => void;
}) {
  const [testProvider, { data, isLoading, error }] = useTestProviderMutation();
  const apiError = error === undefined ? undefined : toApiError(error);

  return (
    <Card>
      <CardHeader pb={0}>
        <Heading size="md">Connection</Heading>
      </CardHeader>
      <CardBody>
        <Stack spacing={4}>
          <HStack>
            <Text>Last test:</Text>
            <LastTestBadge lastTest={provider.last_test} />
            {provider.last_test !== null && (
              <Text color="gray.500" fontSize="sm">
                {new Date(provider.last_test.tested_at).toLocaleString()}
                {provider.last_test.error_code !== null && ` · ${provider.last_test.error_code}`}
              </Text>
            )}
          </HStack>
          {canManage && (
            <Button
              alignSelf="flex-start"
              isLoading={isLoading}
              onClick={() => {
                void testProvider(provider.id)
                  .unwrap()
                  .then(onResult)
                  .catch(() => undefined);
              }}
            >
              Test connection
            </Button>
          )}
          {data !== undefined && (
            <TestOutcome
              ok={data.status === 'ok'}
              title={
                data.status === 'ok' ? `Connected in ${String(data.latency_ms)} ms` : 'Connection failed'
              }
              details={data.status === 'ok' ? `${String(data.models.length)} models available.` : undefined}
              error={data.error}
            />
          )}
          {apiError !== undefined && (
            <ApiErrorAlert error={{ ...apiError, message: messageFor(apiError) }} title="Test not run" />
          )}
        </Stack>
      </CardBody>
    </Card>
  );
}

function ModelsCard({
  provider,
  canManage,
  suggestions,
}: {
  provider: Provider;
  canManage: boolean;
  suggestions: string[];
}) {
  const models = useListModelsQuery(provider.id);
  const [createModel, createState] = useCreateModelMutation();
  const [updateModel] = useUpdateModelMutation();
  const [deleteModel] = useDeleteModelMutation();
  const [modelId, setModelId] = useState('');
  const [displayName, setDisplayName] = useState('');
  const createError = createState.error === undefined ? undefined : toApiError(createState.error);
  const fields = fieldErrorsByName(createError);

  const onAdd = async (event: FormEvent) => {
    event.preventDefault();
    if (modelId.trim() === '') return;
    try {
      await createModel({
        providerId: provider.id,
        model_id: modelId.trim(),
        display_name: displayName.trim() === '' ? null : displayName.trim(),
      }).unwrap();
      setModelId('');
      setDisplayName('');
    } catch {
      // Shown below.
    }
  };

  return (
    <Card>
      <CardHeader pb={0}>
        <Heading size="md">Chat models</Heading>
      </CardHeader>
      <CardBody>
        <Stack spacing={4}>
          {models.isLoading && <PageSpinner />}
          {models.isError && <ApiErrorAlert error={toApiError(models.error)} title="Cannot load models" />}
          {models.data?.items.length === 0 && <Text color="gray.500">No models configured.</Text>}
          {models.data !== undefined && models.data.items.length > 0 && (
            <TableContainer>
              <Table size="sm">
                <Thead>
                  <Tr>
                    <Th>Model ID</Th>
                    <Th>Display name</Th>
                    <Th>Enabled</Th>
                    {canManage && <Th />}
                  </Tr>
                </Thead>
                <Tbody>
                  {models.data.items.map((model) => (
                    <Tr key={model.id}>
                      <Td fontFamily="mono">{model.model_id}</Td>
                      <Td>{model.display_name ?? '—'}</Td>
                      <Td>
                        <Switch
                          aria-label={`Enable ${model.model_id}`}
                          isChecked={model.enabled}
                          isDisabled={!canManage}
                          onChange={(event) => {
                            void updateModel({
                              providerId: provider.id,
                              id: model.id,
                              enabled: event.target.checked,
                            });
                          }}
                        />
                      </Td>
                      {canManage && (
                        <Td>
                          <ConfirmButton
                            label="Remove"
                            confirmLabel="Remove model"
                            onConfirm={() => void deleteModel({ providerId: provider.id, id: model.id })}
                          />
                        </Td>
                      )}
                    </Tr>
                  ))}
                </Tbody>
              </Table>
            </TableContainer>
          )}
          {canManage && (
            <form onSubmit={(event) => void onAdd(event)} noValidate>
              <Stack spacing={2}>
                <HStack align="flex-start">
                  <FormControl isInvalid={fields.model_id !== undefined}>
                    <Input
                      aria-label="Model ID"
                      placeholder="Model ID"
                      list="provider-model-suggestions"
                      value={modelId}
                      onChange={(event) => {
                        setModelId(event.target.value);
                      }}
                    />
                    <FormErrorMessage>{fields.model_id}</FormErrorMessage>
                  </FormControl>
                  <Input
                    aria-label="Display name"
                    placeholder="Display name (optional)"
                    value={displayName}
                    onChange={(event) => {
                      setDisplayName(event.target.value);
                    }}
                  />
                  <Button type="submit" colorScheme="brand" isLoading={createState.isLoading} flexShrink={0}>
                    Add model
                  </Button>
                </HStack>
                <datalist id="provider-model-suggestions">
                  {suggestions.map((id) => (
                    <option key={id} value={id} />
                  ))}
                </datalist>
                {createError !== undefined && Object.keys(fields).length === 0 && (
                  <ApiErrorAlert
                    error={{ ...createError, message: messageFor(createError) }}
                    title="Model not added"
                  />
                )}
              </Stack>
            </form>
          )}
        </Stack>
      </CardBody>
    </Card>
  );
}

function ProviderDetail({ providerId }: { providerId: string }) {
  const navigate = useNavigate();
  const canManage = useHasPermission(PERMISSIONS.providersManage);
  const provider = useGetProviderQuery(providerId);
  const [deleteProvider, deleteState] = useDeleteProviderMutation();
  const [suggestions, setSuggestions] = useState<string[]>([]);
  const deleteError = deleteState.error === undefined ? undefined : toApiError(deleteState.error);

  if (provider.isLoading) return <PageSpinner />;
  if (provider.isError || provider.data === undefined) {
    return <ApiErrorAlert error={toApiError(provider.error)} title="Cannot load the provider" />;
  }
  const data = provider.data;

  return (
    <Stack spacing={6}>
      <Link as={RouterLink} to="/admin/providers" color="brand.500" fontSize="sm">
        ← AI providers
      </Link>
      <HStack justify="space-between" align="flex-start">
        <Stack spacing={1}>
          <Heading size="lg">{data.name}</Heading>
          <HStack>
            <Text color="gray.500">{providerTypeLabel(data.provider_type)}</Text>
            <EnabledBadge enabled={data.enabled} />
          </HStack>
        </Stack>
        {canManage && (
          <ConfirmButton
            label="Delete provider"
            confirmLabel="Delete provider"
            isLoading={deleteState.isLoading}
            onConfirm={() => {
              void deleteProvider(data.id)
                .unwrap()
                .then(() => navigate('/admin/providers'))
                .catch(() => undefined);
            }}
          />
        )}
      </HStack>
      {deleteError !== undefined && (
        <ApiErrorAlert error={{ ...deleteError, message: messageFor(deleteError) }} title="Not deleted" />
      )}
      <DetailsCard key={data.updated_at} provider={data} canManage={canManage} />
      {canManage && <KeyCard provider={data} />}
      <ConnectionCard
        provider={data}
        canManage={canManage}
        onResult={(result) => {
          setSuggestions(result.models);
        }}
      />
      <ModelsCard provider={data} canManage={canManage} suggestions={suggestions} />
    </Stack>
  );
}

export function ProviderDetailPage() {
  const { providerId } = useParams();
  return (
    <RequirePermission permission={PERMISSIONS.providersRead}>
      {providerId !== undefined && <ProviderDetail providerId={providerId} />}
    </RequirePermission>
  );
}
