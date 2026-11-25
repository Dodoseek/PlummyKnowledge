import { Card, CardBody, Heading, SimpleGrid, Stack, Text } from '@chakra-ui/react';

import { toApiError } from '../../shared/api/errors';
import { ApiErrorAlert } from '../../shared/components/ApiErrorAlert';
import { PageSpinner } from '../../shared/components/PageSpinner';
import { Pager } from '../admin/components';
import { useCursorPager } from '../admin/useCursorPager';
import { useListAssistantsQuery } from './assistantsApi';

/**
 * Assistants the current user can use. The backend returns only those (ADR-005) to users without
 * assistants.manage; managers also get the others, which are configured on the admin page.
 */
export function AssistantsPage() {
  const pager = useCursorPager();
  const list = useListAssistantsQuery(pager.cursor !== undefined ? { cursor: pager.cursor } : {});

  let body;
  if (list.isLoading) {
    body = <PageSpinner />;
  } else if (list.isError || list.data === undefined) {
    body = <ApiErrorAlert error={toApiError(list.error)} title="Cannot load assistants" />;
  } else {
    const available = list.data.items.filter((assistant) => assistant.availability.available);
    body = (
      <Stack spacing={4}>
        {available.length === 0 ? (
          <Text color="gray.500">
            No assistant is available to you. An assistant appears here once you have access to all of its
            knowledge bases.
          </Text>
        ) : (
          <SimpleGrid columns={{ base: 1, md: 2 }} spacing={4}>
            {available.map((assistant) => (
              <Card key={assistant.id} variant="outline" data-testid="assistant-card">
                <CardBody>
                  <Stack spacing={1}>
                    <Heading size="sm">{assistant.name}</Heading>
                    {assistant.description !== null && (
                      <Text fontSize="sm" color="gray.500">
                        {assistant.description}
                      </Text>
                    )}
                  </Stack>
                </CardBody>
              </Card>
            ))}
          </SimpleGrid>
        )}
        <Pager pager={pager} nextCursor={list.data.next_cursor} />
      </Stack>
    );
  }

  return (
    <Stack spacing={6}>
      <Stack spacing={2}>
        <Heading size="lg">Assistants</Heading>
        <Text color="gray.500">Chat with assistants arrives in the next release.</Text>
      </Stack>
      {body}
    </Stack>
  );
}
