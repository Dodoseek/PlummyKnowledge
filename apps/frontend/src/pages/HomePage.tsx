import { Heading, Stack, Text } from '@chakra-ui/react';

import { SystemStatusCard } from '../features/health/SystemStatusCard';

/** Technical start page: confirms the frontend runs and can reach the backend. */
export function HomePage() {
  return (
    <Stack spacing={6}>
      <Stack spacing={2}>
        <Heading size="lg">PlummyKnowledge</Heading>
        <Text color="gray.500">Self-hosted AI knowledge base. Development build — foundation stage.</Text>
      </Stack>
      <SystemStatusCard />
    </Stack>
  );
}
