import {
  Button,
  Card,
  CardBody,
  CardHeader,
  FormControl,
  FormErrorMessage,
  FormHelperText,
  FormLabel,
  Heading,
  Input,
  Link,
  Select,
  SimpleGrid,
  Stack,
  Table,
  TableContainer,
  Tbody,
  Td,
  Text,
  Textarea,
  Th,
  Thead,
  Tr,
} from '@chakra-ui/react';
import { useState, type FormEvent } from 'react';
import { Link as RouterLink, useNavigate } from 'react-router';

import { toApiError } from '../../shared/api/errors';
import { fieldErrorsByName, messageFor } from '../../shared/api/messages';
import { ApiErrorAlert } from '../../shared/components/ApiErrorAlert';
import { PageSpinner } from '../../shared/components/PageSpinner';
import { Pager } from '../admin/components';
import { useCursorPager } from '../admin/useCursorPager';
import { PERMISSIONS, useHasPermission } from '../auth/permissions';
import { useListEmbeddingProfilesQuery } from '../providers/providersApi';
import { AccessBadge, IndexingSummary } from './components';
import { useCreateKnowledgeBaseMutation, useListKnowledgeBasesQuery } from './knowledgeApi';

function CreateKnowledgeBaseForm() {
  const navigate = useNavigate();
  const canReadProfiles = useHasPermission(PERMISSIONS.providersRead);
  const profiles = useListEmbeddingProfilesQuery({}, { skip: !canReadProfiles });
  const active = (profiles.data?.items ?? []).filter((profile) => profile.status === 'active');
  const [createKnowledgeBase, { isLoading, error }] = useCreateKnowledgeBaseMutation();
  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  const [profileId, setProfileId] = useState('');
  const apiError = error === undefined ? undefined : toApiError(error);
  const fields = fieldErrorsByName(apiError);
  const selectedProfile = profileId !== '' ? profileId : (active[0]?.id ?? '');

  const onSubmit = async (event: FormEvent) => {
    event.preventDefault();
    try {
      const created = await createKnowledgeBase({
        name: name.trim(),
        ...(description.trim() !== '' ? { description: description.trim() } : {}),
        embedding_profile_id: selectedProfile,
      }).unwrap();
      void navigate(`/knowledge-bases/${encodeURIComponent(created.id)}`);
    } catch {
      // Shown below.
    }
  };

  let body;
  if (!canReadProfiles) {
    body = <Text color="gray.500">Choosing an embedding profile needs the providers.read permission.</Text>;
  } else if (profiles.isLoading) {
    body = <PageSpinner />;
  } else if (profiles.isError) {
    body = <ApiErrorAlert error={toApiError(profiles.error)} title="Cannot load embedding profiles" />;
  } else if (active.length === 0) {
    body = (
      <Text color="gray.500">
        Add an active{' '}
        <Link as={RouterLink} to="/admin/embedding-profiles" color="brand.500">
          embedding profile
        </Link>{' '}
        first.
      </Text>
    );
  } else {
    body = (
      <form onSubmit={(event) => void onSubmit(event)} noValidate>
        <Stack spacing={4}>
          <SimpleGrid columns={{ base: 1, md: 2 }} spacing={4}>
            <FormControl isRequired isInvalid={fields.name !== undefined}>
              <FormLabel>Name</FormLabel>
              <Input
                value={name}
                onChange={(event) => {
                  setName(event.target.value);
                }}
              />
              <FormErrorMessage>{fields.name}</FormErrorMessage>
            </FormControl>
            <FormControl isRequired isInvalid={fields.embedding_profile_id !== undefined}>
              <FormLabel>Embedding profile</FormLabel>
              <Select
                value={selectedProfile}
                onChange={(event) => {
                  setProfileId(event.target.value);
                }}
              >
                {active.map((profile) => (
                  <option key={profile.id} value={profile.id}>
                    {profile.name} ({profile.model_id}, {profile.dimensions})
                  </option>
                ))}
              </Select>
              <FormErrorMessage>{fields.embedding_profile_id}</FormErrorMessage>
              <FormHelperText>
                Cannot change later: another profile needs a new knowledge base.
              </FormHelperText>
            </FormControl>
          </SimpleGrid>
          <FormControl isInvalid={fields.description !== undefined}>
            <FormLabel>Description</FormLabel>
            <Textarea
              value={description}
              rows={2}
              onChange={(event) => {
                setDescription(event.target.value);
              }}
            />
            <FormErrorMessage>{fields.description}</FormErrorMessage>
          </FormControl>
          {apiError !== undefined && Object.keys(fields).length === 0 && (
            <ApiErrorAlert
              error={{ ...apiError, message: messageFor(apiError) }}
              title="Knowledge base not created"
            />
          )}
          <Button type="submit" colorScheme="brand" alignSelf="flex-start" isLoading={isLoading}>
            Create knowledge base
          </Button>
        </Stack>
      </form>
    );
  }

  return (
    <Card>
      <CardHeader pb={0}>
        <Heading size="md">New knowledge base</Heading>
      </CardHeader>
      <CardBody>{body}</CardBody>
    </Card>
  );
}

/** Knowledge bases the user can read; the backend filters the list by access. */
export function KnowledgeBasesPage() {
  const canManage = useHasPermission(PERMISSIONS.knowledgeBasesManage);
  const pager = useCursorPager();
  const list = useListKnowledgeBasesQuery(pager.cursor !== undefined ? { cursor: pager.cursor } : {});

  return (
    <Stack spacing={6}>
      <Heading size="lg">Knowledge bases</Heading>
      <Card>
        <CardBody>
          {list.isLoading && <PageSpinner />}
          {list.isError && (
            <ApiErrorAlert error={toApiError(list.error)} title="Cannot load knowledge bases" />
          )}
          {list.data !== undefined && (
            <Stack spacing={4}>
              {list.data.items.length === 0 ? (
                <Text color="gray.500">
                  {canManage
                    ? 'No knowledge bases yet.'
                    : 'You do not have access to any knowledge base yet.'}
                </Text>
              ) : (
                <TableContainer>
                  <Table size="sm">
                    <Thead>
                      <Tr>
                        <Th>Name</Th>
                        <Th>Documents</Th>
                        <Th>Access</Th>
                      </Tr>
                    </Thead>
                    <Tbody>
                      {list.data.items.map((kb) => (
                        <Tr key={kb.id}>
                          <Td whiteSpace="normal">
                            <Link
                              as={RouterLink}
                              to={`/knowledge-bases/${encodeURIComponent(kb.id)}`}
                              color="brand.500"
                              fontWeight="medium"
                            >
                              {kb.name}
                            </Link>
                            {kb.description !== null && (
                              <Text fontSize="sm" color="gray.500" noOfLines={1}>
                                {kb.description}
                              </Text>
                            )}
                          </Td>
                          <Td>
                            <IndexingSummary indexing={kb.indexing} />
                          </Td>
                          <Td>
                            <AccessBadge level={kb.my_access_level} />
                          </Td>
                        </Tr>
                      ))}
                    </Tbody>
                  </Table>
                </TableContainer>
              )}
              <Pager pager={pager} nextCursor={list.data.next_cursor} />
            </Stack>
          )}
        </CardBody>
      </Card>
      {canManage && <CreateKnowledgeBaseForm />}
    </Stack>
  );
}
