import { Button, Container, Heading, Stack, Text } from '@chakra-ui/react';
import { isRouteErrorResponse, useRouteError } from 'react-router';

/**
 * Fallback for errors thrown while rendering a route. Shows a generic message only: stack traces and error
 * texts are written to the browser console, never rendered.
 */
export function RouteErrorPage() {
  const error = useRouteError();
  console.error(error);
  const status = isRouteErrorResponse(error) ? error.status : undefined;

  return (
    <Container maxW="container.md" py={16}>
      <Stack spacing={4} align="flex-start">
        <Heading size="lg">Something went wrong</Heading>
        <Text color="gray.500">
          {status !== undefined ? `Error ${status}. ` : ''}
          The page could not be displayed. Reload the page or try again later.
        </Text>
        <Button
          colorScheme="brand"
          onClick={() => {
            window.location.assign('/');
          }}
        >
          Go to home page
        </Button>
      </Stack>
    </Container>
  );
}
