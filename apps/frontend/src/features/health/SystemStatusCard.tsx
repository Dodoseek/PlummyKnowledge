import {
  Badge,
  Button,
  Card,
  CardBody,
  CardHeader,
  Flex,
  Heading,
  List,
  ListItem,
  Spinner,
  Stack,
  Text,
} from '@chakra-ui/react';

import { toApiError } from '../../shared/api/errors';
import { ApiErrorAlert } from '../../shared/components/ApiErrorAlert';
import { useGetReadinessQuery, type CheckStatus } from './healthApi';

const CHECK_LABELS: Record<string, string> = {
  database: 'PostgreSQL',
  migrations: 'Database migrations',
  redis: 'Redis',
};

function StatusBadge({ status }: { status: CheckStatus }) {
  return <Badge colorScheme={status === 'ok' ? 'green' : 'red'}>{status === 'ok' ? 'OK' : 'Failed'}</Badge>;
}

/** Shows whether the backend is reachable and ready (readiness endpoint). */
export function SystemStatusCard() {
  const { data, error, isLoading, isFetching, refetch } = useGetReadinessQuery();

  return (
    <Card variant="outline">
      <CardHeader pb={0}>
        <Flex align="center" justify="space-between" gap={4}>
          <Heading size="md">System status</Heading>
          <Button
            size="sm"
            onClick={() => {
              void refetch();
            }}
            isLoading={isFetching}
            loadingText="Checking"
          >
            Refresh
          </Button>
        </Flex>
      </CardHeader>
      <CardBody>
        {isLoading ? (
          <Flex align="center" gap={3} role="status">
            <Spinner size="sm" />
            <Text>Checking backend…</Text>
          </Flex>
        ) : error ? (
          <ApiErrorAlert error={toApiError(error)} title="Backend is unavailable" />
        ) : data ? (
          <Stack spacing={3}>
            <Flex align="center" gap={2}>
              <Text fontWeight="semibold">Backend API:</Text>
              <Badge
                colorScheme={data.status === 'ready' ? 'green' : 'orange'}
                data-testid="readiness-status"
              >
                {data.status === 'ready' ? 'Ready' : 'Not ready'}
              </Badge>
            </Flex>
            {data.checks && (
              <List spacing={1} aria-label="Dependency checks">
                {Object.entries(data.checks).map(([name, status]) => (
                  <ListItem key={name}>
                    <Flex align="center" gap={2}>
                      <Text minW="12rem">{CHECK_LABELS[name] ?? name}</Text>
                      <StatusBadge status={status} />
                    </Flex>
                  </ListItem>
                ))}
              </List>
            )}
          </Stack>
        ) : null}
      </CardBody>
    </Card>
  );
}
