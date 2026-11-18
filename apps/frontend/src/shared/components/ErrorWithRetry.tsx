import { Button, Stack } from '@chakra-ui/react';

import type { ApiError } from '../api/errors';
import { messageFor } from '../api/messages';
import { ApiErrorAlert } from './ApiErrorAlert';

interface ErrorWithRetryProps {
  error: ApiError;
  title: string;
  onRetry: () => void;
}

export function ErrorWithRetry({ error, title, onRetry }: ErrorWithRetryProps) {
  return (
    <Stack spacing={4} align="flex-start">
      <ApiErrorAlert error={{ ...error, message: messageFor(error) }} title={title} />
      <Button onClick={onRetry} variant="outline">
        Try again
      </Button>
    </Stack>
  );
}
