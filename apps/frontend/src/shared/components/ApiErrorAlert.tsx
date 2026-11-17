import { Alert, AlertDescription, AlertIcon, AlertTitle, Box, Code, Text } from '@chakra-ui/react';

import type { ApiError } from '../api/errors';

interface ApiErrorAlertProps {
  error: ApiError;
  title?: string;
}

export function ApiErrorAlert({ error, title = 'Request failed' }: ApiErrorAlertProps) {
  return (
    <Alert status="error" borderRadius="md" alignItems="flex-start">
      <AlertIcon />
      <Box>
        <AlertTitle>{title}</AlertTitle>
        <AlertDescription>
          <Text>{error.message}</Text>
          {error.correlationId && (
            <Text fontSize="sm" mt={1}>
              Request ID: <Code>{error.correlationId}</Code>
            </Text>
          )}
        </AlertDescription>
      </Box>
    </Alert>
  );
}
