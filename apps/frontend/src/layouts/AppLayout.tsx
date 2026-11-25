import { Box, Container, Flex, Heading, HStack, Text } from '@chakra-ui/react';
import { Link as RouterLink, Outlet } from 'react-router';

import { UserMenu } from '../features/auth/UserMenu';
import { config } from '../shared/config/env';

export function AppLayout() {
  return (
    <Flex direction="column" minH="100vh">
      <Box as="header" borderBottomWidth="1px" py={3}>
        <Container maxW="container.lg">
          <Flex align="center" justify="space-between" gap={4}>
            <Heading as={RouterLink} to="/" size="md" color="brand.500">
              PlummyKnowledge
            </Heading>
            <HStack spacing={4}>
              <UserMenu />
              <Text fontSize="sm" color="gray.500" data-testid="app-version">
                v{config.appVersion}
              </Text>
            </HStack>
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
