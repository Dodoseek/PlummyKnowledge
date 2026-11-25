import {
  Alert,
  AlertDescription,
  AlertIcon,
  Card,
  CardBody,
  HStack,
  Heading,
  Link,
  Stack,
  Text,
} from '@chakra-ui/react';
import { useState } from 'react';
import { Link as RouterLink, useNavigate, useParams } from 'react-router';

import { toApiError } from '../../shared/api/errors';
import { messageFor } from '../../shared/api/messages';
import { ApiErrorAlert } from '../../shared/components/ApiErrorAlert';
import { PageSpinner } from '../../shared/components/PageSpinner';
import { ConfirmButton } from '../admin/components';
import { RequirePermission } from '../auth/RequirePermission';
import { PERMISSIONS } from '../auth/permissions';
import { AssistantForm } from './AssistantForm';
import {
  useDeleteAssistantMutation,
  useGetAssistantQuery,
  useUpdateAssistantMutation,
} from './assistantsApi';
import { AvailabilityBadge } from './components';

function AssistantDetail({ id }: { id: string }) {
  const navigate = useNavigate();
  const assistant = useGetAssistantQuery(id);
  const [updateAssistant, update] = useUpdateAssistantMutation();
  const [deleteAssistant, remove] = useDeleteAssistantMutation();
  const [saved, setSaved] = useState(false);

  if (assistant.isLoading) return <PageSpinner />;
  if (assistant.isError || assistant.data === undefined) {
    return <ApiErrorAlert error={toApiError(assistant.error)} title="Assistant not available" />;
  }
  const data = assistant.data;

  return (
    <Stack spacing={6}>
      <Stack spacing={2}>
        <HStack spacing={3} flexWrap="wrap">
          <Heading size="lg">{data.name}</Heading>
          <AvailabilityBadge assistant={data} />
        </HStack>
        {data.description !== null && <Text color="gray.500">{data.description}</Text>}
      </Stack>
      <Card>
        <CardBody>
          <Stack spacing={4}>
            {saved && !update.isLoading && update.error === undefined && (
              <Alert status="success" borderRadius="md">
                <AlertIcon />
                <AlertDescription>Saved.</AlertDescription>
              </Alert>
            )}
            <AssistantForm
              // A fresh form after every save shows the values the server stored.
              key={data.updated_at}
              assistant={data}
              submitLabel="Save"
              isLoading={update.isLoading}
              error={update.error}
              onSubmit={(body) => {
                setSaved(false);
                updateAssistant({ id, body })
                  .unwrap()
                  .then(() => {
                    setSaved(true);
                  })
                  .catch(() => {
                    // Shown by the form.
                  });
              }}
            />
          </Stack>
        </CardBody>
      </Card>
      <Stack spacing={2}>
        <Text fontWeight="medium">Delete</Text>
        <HStack>
          <ConfirmButton
            label="Delete assistant"
            confirmLabel="Delete for good"
            isLoading={remove.isLoading}
            onConfirm={() => {
              deleteAssistant(id)
                .unwrap()
                .then(() => navigate('/admin/assistants', { replace: true }))
                .catch(() => {
                  // Shown below.
                });
            }}
          />
        </HStack>
        {remove.error !== undefined && (
          <ApiErrorAlert
            error={{ ...toApiError(remove.error), message: messageFor(toApiError(remove.error)) }}
            title="Not deleted"
          />
        )}
      </Stack>
    </Stack>
  );
}

export function AssistantDetailPage() {
  const { assistantId = '' } = useParams();
  return (
    <RequirePermission permission={PERMISSIONS.assistantsManage}>
      <Stack spacing={4}>
        <Link as={RouterLink} to="/admin/assistants" color="brand.500" fontSize="sm">
          ← Assistants
        </Link>
        <AssistantDetail id={assistantId} />
      </Stack>
    </RequirePermission>
  );
}
