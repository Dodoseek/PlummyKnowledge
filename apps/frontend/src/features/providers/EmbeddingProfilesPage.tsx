import {
  Button,
  Card,
  CardBody,
  CardHeader,
  Checkbox,
  FormControl,
  FormErrorMessage,
  FormHelperText,
  FormLabel,
  Heading,
  HStack,
  Input,
  Link,
  NumberInput,
  NumberInputField,
  Select,
  SimpleGrid,
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
import { Link as RouterLink } from 'react-router';

import { toApiError } from '../../shared/api/errors';
import { fieldErrorsByName, messageFor } from '../../shared/api/messages';
import { ApiErrorAlert } from '../../shared/components/ApiErrorAlert';
import { PageSpinner } from '../../shared/components/PageSpinner';
import { Pager } from '../admin/components';
import { useCursorPager } from '../admin/useCursorPager';
import { PERMISSIONS, useHasPermission } from '../auth/permissions';
import { RequirePermission } from '../auth/RequirePermission';
import { TestOutcome } from './components';
import {
  EMBEDDING_PROVIDER_TYPES,
  useCreateEmbeddingProfileMutation,
  useListEmbeddingProfilesQuery,
  useListProvidersQuery,
  useTestEmbeddingProfileMutation,
  useUpdateEmbeddingProfileMutation,
  type DistanceMetric,
  type EmbeddingProfile,
  type Provider,
  type ProfileStatus,
} from './providersApi';

const METRICS: { value: DistanceMetric; label: string }[] = [
  { value: 'cosine', label: 'Cosine' },
  { value: 'inner_product', label: 'Inner product' },
  { value: 'l2', label: 'Euclidean (L2)' },
];

const STATUSES: ProfileStatus[] = ['active', 'deprecated', 'retired'];

function CreateProfileForm({ providers }: { providers: Provider[] }) {
  const [createProfile, { isLoading, error }] = useCreateEmbeddingProfileMutation();
  const candidates = providers.filter((provider) =>
    EMBEDDING_PROVIDER_TYPES.includes(provider.provider_type),
  );
  const [name, setName] = useState('');
  const [providerId, setProviderId] = useState('');
  const [modelId, setModelId] = useState('');
  const [dimensions, setDimensions] = useState('');
  const [metric, setMetric] = useState<DistanceMetric>('cosine');
  const [normalize, setNormalize] = useState(false);
  const apiError = error === undefined ? undefined : toApiError(error);
  const fields = fieldErrorsByName(apiError);
  const selectedProvider = providerId !== '' ? providerId : (candidates[0]?.id ?? '');

  const onSubmit = async (event: FormEvent) => {
    event.preventDefault();
    try {
      await createProfile({
        name: name.trim(),
        provider_id: selectedProvider,
        model_id: modelId.trim(),
        dimensions: Number(dimensions),
        distance_metric: metric,
        normalize,
      }).unwrap();
      setName('');
      setModelId('');
      setDimensions('');
    } catch {
      // Shown below.
    }
  };

  return (
    <Card>
      <CardHeader pb={0}>
        <Heading size="md">Add embedding profile</Heading>
      </CardHeader>
      <CardBody>
        {candidates.length === 0 ? (
          <Text color="gray.500">Add an OpenAI, Ollama or OpenAI-compatible provider first.</Text>
        ) : (
          <form onSubmit={(event) => void onSubmit(event)} noValidate>
            <Stack spacing={4}>
              <SimpleGrid columns={{ base: 1, md: 2 }} spacing={4}>
                <FormControl isRequired isInvalid={fields.name !== undefined}>
                  <FormLabel>Name</FormLabel>
                  <Input
                    value={name}
                    onChange={(event) => {
                      setName(event.target.value);
                    }}
                  />
                  <FormErrorMessage>{fields.name}</FormErrorMessage>
                </FormControl>
                <FormControl isRequired isInvalid={fields.provider_id !== undefined}>
                  <FormLabel>Provider</FormLabel>
                  <Select
                    value={selectedProvider}
                    onChange={(event) => {
                      setProviderId(event.target.value);
                    }}
                  >
                    {candidates.map((provider) => (
                      <option key={provider.id} value={provider.id}>
                        {provider.name}
                      </option>
                    ))}
                  </Select>
                  <FormErrorMessage>{fields.provider_id}</FormErrorMessage>
                </FormControl>
                <FormControl isRequired isInvalid={fields.model_id !== undefined}>
                  <FormLabel>Model ID</FormLabel>
                  <Input
                    value={modelId}
                    placeholder="text-embedding-3-small"
                    onChange={(event) => {
                      setModelId(event.target.value);
                    }}
                  />
                  <FormErrorMessage>{fields.model_id}</FormErrorMessage>
                </FormControl>
                <FormControl isRequired isInvalid={fields.dimensions !== undefined}>
                  <FormLabel>Dimensions</FormLabel>
                  <NumberInput
                    min={1}
                    max={16000}
                    value={dimensions}
                    onChange={(value) => {
                      setDimensions(value);
                    }}
                  >
                    <NumberInputField />
                  </NumberInput>
                  <FormErrorMessage>{fields.dimensions}</FormErrorMessage>
                  <FormHelperText>Use “Test” after creating to check it against the model.</FormHelperText>
                </FormControl>
                <FormControl>
                  <FormLabel>Distance metric</FormLabel>
                  <Select
                    value={metric}
                    onChange={(event) => {
                      setMetric(event.target.value as DistanceMetric);
                    }}
                  >
                    {METRICS.map((item) => (
                      <option key={item.value} value={item.value}>
                        {item.label}
                      </option>
                    ))}
                  </Select>
                </FormControl>
                <Checkbox
                  alignSelf="flex-end"
                  isChecked={normalize}
                  onChange={(event) => {
                    setNormalize(event.target.checked);
                  }}
                >
                  Normalize vectors
                </Checkbox>
              </SimpleGrid>
              <Text fontSize="sm" color="gray.500">
                Model, dimensions and metric cannot change later: a different model needs a new profile and a
                reindex.
              </Text>
              {apiError !== undefined && Object.keys(fields).length === 0 && (
                <ApiErrorAlert
                  error={{ ...apiError, message: messageFor(apiError) }}
                  title="Profile not added"
                />
              )}
              <Button type="submit" colorScheme="brand" alignSelf="flex-start" isLoading={isLoading}>
                Add profile
              </Button>
            </Stack>
          </form>
        )}
      </CardBody>
    </Card>
  );
}

function ProfileRow({
  profile,
  providerName,
  canManage,
}: {
  profile: EmbeddingProfile;
  providerName: string;
  canManage: boolean;
}) {
  const [updateProfile, updateState] = useUpdateEmbeddingProfileMutation();
  const [testProfile, testState] = useTestEmbeddingProfileMutation();
  const updateError = updateState.error === undefined ? undefined : toApiError(updateState.error);
  const testError = testState.error === undefined ? undefined : toApiError(testState.error);
  const result = testState.data;

  return (
    <>
      <Tr>
        <Td>{profile.name}</Td>
        <Td>{providerName}</Td>
        <Td fontFamily="mono">{profile.model_id}</Td>
        <Td isNumeric>{profile.dimensions}</Td>
        <Td>{profile.distance_metric}</Td>
        <Td>
          <Select
            size="sm"
            aria-label={`Status of ${profile.name}`}
            value={profile.status}
            isDisabled={!canManage || profile.status === 'retired'}
            onChange={(event) => {
              void updateProfile({ id: profile.id, status: event.target.value as ProfileStatus });
            }}
          >
            {STATUSES.map((status) => (
              <option key={status} value={status}>
                {status}
              </option>
            ))}
          </Select>
        </Td>
        {canManage && (
          <Td>
            <Button size="sm" isLoading={testState.isLoading} onClick={() => void testProfile(profile.id)}>
              Test
            </Button>
          </Td>
        )}
      </Tr>
      {(result !== undefined || testError !== undefined || updateError !== undefined) && (
        <Tr>
          <Td colSpan={canManage ? 7 : 6} whiteSpace="normal">
            {result !== undefined && (
              <TestOutcome
                ok={result.status === 'ok'}
                title={
                  result.status === 'ok'
                    ? `Vectors of ${String(result.actual_dimensions)} dimensions, as expected`
                    : 'Embedding test failed'
                }
                details={
                  result.actual_dimensions !== null && result.status !== 'ok'
                    ? `Expected ${String(result.expected_dimensions)}, got ${String(result.actual_dimensions)}.`
                    : undefined
                }
                error={result.error}
              />
            )}
            {testError !== undefined && (
              <ApiErrorAlert error={{ ...testError, message: messageFor(testError) }} title="Test not run" />
            )}
            {updateError !== undefined && (
              <ApiErrorAlert error={{ ...updateError, message: messageFor(updateError) }} title="Not saved" />
            )}
          </Td>
        </Tr>
      )}
    </>
  );
}

function ProfilesList() {
  const canManage = useHasPermission(PERMISSIONS.providersManage);
  const pager = useCursorPager();
  const profiles = useListEmbeddingProfilesQuery(pager.cursor !== undefined ? { cursor: pager.cursor } : {});
  const providers = useListProvidersQuery({});
  const providerItems = providers.data?.items ?? [];
  const names = new Map(providerItems.map((provider) => [provider.id, provider.name]));

  return (
    <Stack spacing={6}>
      <Link as={RouterLink} to="/admin/providers" color="brand.500" fontSize="sm">
        ← AI providers
      </Link>
      <Heading size="lg">Embedding profiles</Heading>
      <Card>
        <CardBody>
          {profiles.isLoading && <PageSpinner />}
          {profiles.isError && (
            <ApiErrorAlert error={toApiError(profiles.error)} title="Cannot load profiles" />
          )}
          {profiles.data !== undefined && (
            <Stack spacing={4}>
              <TableContainer>
                <Table size="sm">
                  <Thead>
                    <Tr>
                      <Th>Name</Th>
                      <Th>Provider</Th>
                      <Th>Model</Th>
                      <Th isNumeric>Dimensions</Th>
                      <Th>Metric</Th>
                      <Th>Status</Th>
                      {canManage && <Th />}
                    </Tr>
                  </Thead>
                  <Tbody>
                    {profiles.data.items.map((profile) => (
                      <ProfileRow
                        key={profile.id}
                        profile={profile}
                        providerName={names.get(profile.provider_id) ?? '—'}
                        canManage={canManage}
                      />
                    ))}
                  </Tbody>
                </Table>
              </TableContainer>
              {profiles.data.items.length === 0 && <Text color="gray.500">No embedding profiles yet.</Text>}
              <Pager pager={pager} nextCursor={profiles.data.next_cursor} />
            </Stack>
          )}
        </CardBody>
      </Card>
      {canManage && providers.data !== undefined && (
        <HStack align="stretch">
          <CreateProfileForm providers={providerItems} />
        </HStack>
      )}
    </Stack>
  );
}

export function EmbeddingProfilesPage() {
  return (
    <RequirePermission permission={PERMISSIONS.providersRead}>
      <ProfilesList />
    </RequirePermission>
  );
}
