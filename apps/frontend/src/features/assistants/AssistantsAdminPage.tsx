import {
  Card,
  CardBody,
  CardHeader,
  Heading,
  Link,
  Stack,
  Table,
  TableContainer,
  Tbody,
  Td,
  Text,
  Th,
  Thead,
  Tr,
} from '@chakra-ui/react';
import { Link as RouterLink, useNavigate } from 'react-router';

import { toApiError } from '../../shared/api/errors';
import { ApiErrorAlert } from '../../shared/components/ApiErrorAlert';
import { PageSpinner } from '../../shared/components/PageSpinner';
import { Pager } from '../admin/components';
import { useCursorPager } from '../admin/useCursorPager';
import { RequirePermission } from '../auth/RequirePermission';
import { PERMISSIONS } from '../auth/permissions';
import { AssistantForm } from './AssistantForm';
import { useCreateAssistantMutation, useListAssistantsQuery } from './assistantsApi';
import { AvailabilityBadge } from './components';

function CreateAssistantCard() {
  const navigate = useNavigate();
  const [createAssistant, { isLoading, error }] = useCreateAssistantMutation();
  return (
    <Card>
      <CardHeader pb={0}>
        <Heading size="md">New assistant</Heading>
      </CardHeader>
      <CardBody>
        <AssistantForm
          submitLabel="Create assistant"
          isLoading={isLoading}
          error={error}
          onSubmit={(body) => {
            createAssistant(body)
              .unwrap()
              .then((created) => navigate(`/admin/assistants/${encodeURIComponent(created.id)}`))
              .catch(() => {
                // Shown by the form.
              });
          }}
        />
      </CardBody>
    </Card>
  );
}

function AssistantsTable() {
  const pager = useCursorPager();
  const list = useListAssistantsQuery(pager.cursor !== undefined ? { cursor: pager.cursor } : {});
  if (list.isLoading) return <PageSpinner />;
  if (list.isError || list.data === undefined) {
    return <ApiErrorAlert error={toApiError(list.error)} title="Cannot load assistants" />;
  }
  return (
    <Stack spacing={4}>
      {list.data.items.length === 0 ? (
        <Text color="gray.500">No assistants yet.</Text>
      ) : (
        <TableContainer>
          <Table size="sm">
            <Thead>
              <Tr>
                <Th>Name</Th>
                <Th>Status</Th>
                <Th isNumeric>Knowledge bases</Th>
                <Th>For you</Th>
              </Tr>
            </Thead>
            <Tbody>
              {list.data.items.map((assistant) => (
                <Tr key={assistant.id}>
                  <Td whiteSpace="normal">
                    <Link
                      as={RouterLink}
                      to={`/admin/assistants/${encodeURIComponent(assistant.id)}`}
                      color="brand.500"
                    >
                      {assistant.name}
                    </Link>
                  </Td>
                  <Td>{assistant.status}</Td>
                  <Td isNumeric>{assistant.knowledge_base_ids.length}</Td>
                  <Td whiteSpace="normal">
                    <AvailabilityBadge assistant={assistant} />
                  </Td>
                </Tr>
              ))}
            </Tbody>
          </Table>
        </TableContainer>
      )}
      <Pager pager={pager} nextCursor={list.data.next_cursor} />
    </Stack>
  );
}

export function AssistantsAdminPage() {
  return (
    <RequirePermission permission={PERMISSIONS.assistantsManage}>
      <Stack spacing={6}>
        <Stack spacing={2}>
          <Heading size="lg">Assistants</Heading>
          <Text color="gray.500">
            Who can use an assistant follows from access to all of its knowledge bases; it is checked on every
            request.
          </Text>
        </Stack>
        <Card>
          <CardBody>
            <AssistantsTable />
          </CardBody>
        </Card>
        <CreateAssistantCard />
      </Stack>
    </RequirePermission>
  );
}
