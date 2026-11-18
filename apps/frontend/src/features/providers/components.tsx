import { Alert, AlertDescription, AlertIcon, AlertTitle, Badge, Box, Text } from '@chakra-ui/react';

import type { Provider, TestError } from './providersApi';

export function EnabledBadge({ enabled }: { enabled: boolean }) {
  return <Badge colorScheme={enabled ? 'green' : 'gray'}>{enabled ? 'enabled' : 'disabled'}</Badge>;
}

export function LastTestBadge({ lastTest }: { lastTest: Provider['last_test'] }) {
  if (lastTest === null) return <Badge>not tested</Badge>;
  return (
    <Badge colorScheme={lastTest.status === 'ok' ? 'green' : 'red'} title={lastTest.error_code ?? undefined}>
      {lastTest.status === 'ok' ? 'connected' : 'failed'}
    </Badge>
  );
}

/** The key itself is never available to the browser after it was saved; only a masked hint is. */
export function KeySummary({ provider }: { provider: Provider }) {
  if (!provider.has_secret) return <Text color="gray.500">No key</Text>;
  return <Text fontFamily="mono">{provider.secret_hint ?? 'Key set'}</Text>;
}

/** Outcome of a connection or embedding test. Errors carry only a typed code and a fixed message. */
export function TestOutcome({
  ok,
  title,
  details,
  error,
}: {
  ok: boolean;
  title: string;
  details?: string | undefined;
  error: TestError | null;
}) {
  return (
    <Alert
      status={ok ? 'success' : 'error'}
      borderRadius="md"
      alignItems="flex-start"
      data-testid="test-outcome"
    >
      <AlertIcon />
      <Box>
        <AlertTitle>{title}</AlertTitle>
        <AlertDescription>
          {error !== null && (
            <Text>
              {error.message}{' '}
              <Text as="span" color="gray.500">
                ({error.code})
              </Text>
            </Text>
          )}
          {details !== undefined && <Text>{details}</Text>}
        </AlertDescription>
      </Box>
    </Alert>
  );
}
