import {
  Button,
  Card,
  CardBody,
  CardHeader,
  FormControl,
  FormErrorMessage,
  FormHelperText,
  FormLabel,
  Heading,
  HStack,
  Input,
  Link,
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
import { Link as RouterLink, useNavigate } from 'react-router';

import { toApiError, type ApiError } from '../../shared/api/errors';
import { fieldErrorsByName, messageFor } from '../../shared/api/messages';
import { ApiErrorAlert } from '../../shared/components/ApiErrorAlert';
import { PageSpinner } from '../../shared/components/PageSpinner';
import { Pager } from '../admin/components';
import { useCursorPager } from '../admin/useCursorPager';
import { PERMISSIONS, useHasPermission } from '../auth/permissions';
import { RequirePermission } from '../auth/RequirePermission';
import { EnabledBadge, KeySummary, LastTestBadge } from './components';
import {
  PROVIDER_TYPES,
  providerTypeLabel,
  useCreateProviderMutation,
  useListProvidersQuery,
  type CreateProviderBody,
  type ProviderType,
} from './providersApi';

function CreateProviderForm() {
  const navigate = useNavigate();
  const [createProvider, { isLoading, reset }] = useCreateProviderMutation();
  const [name, setName] = useState('');
  const [type, setType] = useState<ProviderType>('openai');
  const [baseUrl, setBaseUrl] = useState('');
  // Write-only: lives in this component only, never in the store, storage or the URL.
  const [apiKey, setApiKey] = useState('');
  const [error, setError] = useState<ApiError>();
  const fields = fieldErrorsByName(error);
  const meta = PROVIDER_TYPES.find((item) => item.value === type);

  const onSubmit = async (event: FormEvent) => {
    event.preventDefault();
    const body: CreateProviderBody = { name: name.trim(), provider_type: type };
    if (baseUrl.trim() !== '') body.base_url = baseUrl.trim();
    if (apiKey !== '') body.api_key = apiKey;
    setError(undefined);
    try {
      const provider = await createProvider(body).unwrap();
      setApiKey('');
      void navigate(`/admin/providers/${provider.id}`);
    } catch (caught) {
      setError(toApiError(caught as Parameters<typeof toApiError>[0]));
    } finally {
      reset(); // drop the request (and the key) from the RTK Query cache at once
    }
  };

  return (
    <Card>
      <CardHeader pb={0}>
        <Heading size="md">Add provider</Heading>
      </CardHeader>
      <CardBody>
        <form onSubmit={(event) => void onSubmit(event)} noValidate autoComplete="off">
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
              <FormControl isRequired>
                <FormLabel>Type</FormLabel>
                <Select
                  value={type}
                  onChange={(event) => {
                    setType(event.target.value as ProviderType);
                  }}
                >
                  {PROVIDER_TYPES.map((item) => (
                    <option key={item.value} value={item.value}>
                      {item.label}
                    </option>
                  ))}
                </Select>
              </FormControl>
              <FormControl
                isRequired={meta?.defaultUrl === undefined}
                isInvalid={fields.base_url !== undefined}
              >
                <FormLabel>Base URL</FormLabel>
                <Input
                  value={baseUrl}
                  placeholder={meta?.defaultUrl ?? 'http://ollama.internal:11434'}
                  onChange={(event) => {
                    setBaseUrl(event.target.value);
                  }}
                />
                <FormErrorMessage>{fields.base_url}</FormErrorMessage>
                <FormHelperText>
                  Internal addresses work only if the server operator allowlisted them.
                </FormHelperText>
              </FormControl>
              <FormControl isRequired={meta?.keyRequired ?? false} isInvalid={fields.api_key !== undefined}>
                <FormLabel>API key</FormLabel>
                <Input
                  type="password"
                  autoComplete="off"
                  spellCheck={false}
                  value={apiKey}
                  onChange={(event) => {
                    setApiKey(event.target.value);
                  }}
                />
                <FormErrorMessage>{fields.api_key}</FormErrorMessage>
                <FormHelperText>Stored encrypted. It is never shown again.</FormHelperText>
              </FormControl>
            </SimpleGrid>
            {error !== undefined && Object.keys(fields).length === 0 && (
              <ApiErrorAlert error={{ ...error, message: messageFor(error) }} title="Provider not added" />
            )}
            {error?.code === 'endpoint_not_allowed' && (
              <Text fontSize="sm" color="red.500">
                {messageFor(error)}
              </Text>
            )}
            <Button type="submit" colorScheme="brand" alignSelf="flex-start" isLoading={isLoading}>
              Add provider
            </Button>
          </Stack>
        </form>
      </CardBody>
    </Card>
  );
}

function ProvidersList() {
  const canManage = useHasPermission(PERMISSIONS.providersManage);
  const pager = useCursorPager();
  const providers = useListProvidersQuery(pager.cursor !== undefined ? { cursor: pager.cursor } : {});

  return (
    <Stack spacing={6}>
      <HStack justify="space-between">
        <Heading size="lg">AI providers</Heading>
        <Link as={RouterLink} to="/admin/embedding-profiles" color="brand.500">
          Embedding profiles
        </Link>
      </HStack>
      <Card>
        <CardBody>
          {providers.isLoading && <PageSpinner />}
          {providers.isError && (
            <ApiErrorAlert error={toApiError(providers.error)} title="Cannot load providers" />
          )}
          {providers.data !== undefined && (
            <Stack spacing={4}>
              <TableContainer>
                <Table size="sm">
                  <Thead>
                    <Tr>
                      <Th>Name</Th>
                      <Th>Type</Th>
                      <Th>Base URL</Th>
                      <Th>Key</Th>
                      <Th>Status</Th>
                    </Tr>
                  </Thead>
                  <Tbody>
                    {providers.data.items.map((provider) => (
                      <Tr key={provider.id}>
                        <Td>
                          <Link as={RouterLink} to={`/admin/providers/${provider.id}`} color="brand.500">
                            {provider.name}
                          </Link>
                        </Td>
                        <Td>{providerTypeLabel(provider.provider_type)}</Td>
                        <Td maxW="xs" whiteSpace="normal" wordBreak="break-all">
                          {provider.base_url}
                        </Td>
                        <Td>
                          <KeySummary provider={provider} />
                        </Td>
                        <Td>
                          <HStack>
                            <EnabledBadge enabled={provider.enabled} />
                            <LastTestBadge lastTest={provider.last_test} />
                          </HStack>
                        </Td>
                      </Tr>
                    ))}
                  </Tbody>
                </Table>
              </TableContainer>
              {providers.data.items.length === 0 && <Text color="gray.500">No providers yet.</Text>}
              <Pager pager={pager} nextCursor={providers.data.next_cursor} />
            </Stack>
          )}
        </CardBody>
      </Card>
      {canManage && <CreateProviderForm />}
    </Stack>
  );
}

export function ProvidersPage() {
  return (
    <RequirePermission permission={PERMISSIONS.providersRead}>
      <ProvidersList />
    </RequirePermission>
  );
}
