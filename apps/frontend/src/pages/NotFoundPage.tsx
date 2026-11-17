import { Button, Heading, Stack, Text } from '@chakra-ui/react';
import { Link as RouterLink } from 'react-router';

export function NotFoundPage() {
  return (
    <Stack spacing={4} align="flex-start">
      <Heading size="lg">Page not found</Heading>
      <Text color="gray.500">The page you are looking for does not exist.</Text>
      <Button as={RouterLink} to="/" colorScheme="brand">
        Go to home page
      </Button>
    </Stack>
  );
}
