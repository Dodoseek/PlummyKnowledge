import { Box, Container, Flex, Heading, Text } from '@chakra-ui/react';
import { Link as RouterLink, Outlet } from 'react-router';

import { config } from '../shared/config/env';

export function AppLayout() {
  return (
    <Flex direction="column" minH="100vh">
      <Box as="header" borderBottomWidth="1px" py={3}>
        <Container maxW="container.lg">
          <Flex align="center" justify="space-between">
            <Heading as={RouterLink} to="/" size="md" color="brand.500">
              PlummyKnowledge
            </Heading>
            <Text fontSize="sm" color="gray.500" data-testid="app-version">
              v{config.appVersion}
            </Text>
          </Flex>
        </Container>
      </Box>
      <Box as="main" flex="1" py={8}>
        <Container maxW="container.lg">
          <Outlet />
        </Container>
      </Box>
    </Flex>
  );
}
