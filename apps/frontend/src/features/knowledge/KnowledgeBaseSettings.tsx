import {
  Alert,
  AlertDescription,
  AlertIcon,
  Badge,
  Button,
  Card,
  CardBody,
  CardHeader,
  FormControl,
  FormErrorMessage,
  FormHelperText,
  FormLabel,
  Heading,
  HStack,
  Input,
  List,
  ListItem,
  Select,
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
import type { SerializedError } from '@reduxjs/toolkit';
import type { FetchBaseQueryError } from '@reduxjs/toolkit/query';
import { useState, type FormEvent } from 'react';
import { useNavigate } from 'react-router';

import { toApiError, type ApiError } from '../../shared/api/errors';
import { fieldErrorsByName, messageFor } from '../../shared/api/messages';
import { ApiErrorAlert } from '../../shared/components/ApiErrorAlert';
import { PageSpinner } from '../../shared/components/PageSpinner';
import { useListGroupsQuery, useListUsersQuery } from '../admin/adminApi';
import { ConfirmButton } from '../admin/components';
import { PERMISSIONS, useHasPermission } from '../auth/permissions';
import {
  useDeleteKnowledgeBaseMutation,
  useGetKnowledgeBaseAccessQuery,
  useReindexKnowledgeBaseMutation,
  useReplaceKnowledgeBaseAccessMutation,
  useUpdateKnowledgeBaseMutation,
  type AccessLevel,
  type KbGrant,
  type KnowledgeBase,
  type PrincipalType,
} from './knowledgeApi';

const SEARCH_LIMIT = 10;

/** A rejected field carries the precise reason; other errors map by code. */
function errorMessage(error: FetchBaseQueryError | SerializedError): string {
  const apiError = toApiError(error);
  return apiError.fieldErrors?.[0]?.message ?? messageFor(apiError);
}

function withMessage(apiError: ApiError): ApiError {
  return { ...apiError, message: messageFor(apiError) };
}

/** Name and description, reindexing of every document and deletion. Shown to managers of the knowledge base. */
export function KnowledgeBaseSettingsCard({ kb }: { kb: KnowledgeBase }) {
  const navigate = useNavigate();
  // Deleting a knowledge base needs the permission, not only `manage` on it; the backend checks it again.
  const canDelete = useHasPermission(PERMISSIONS.knowledgeBasesManage);
  const [updateKnowledgeBase, update] = useUpdateKnowledgeBaseMutation();
  const [reindexKnowledgeBase, reindex] = useReindexKnowledgeBaseMutation();
  const [deleteKnowledgeBase, remove] = useDeleteKnowledgeBaseMutation();
  const [name, setName] = useState(kb.name);
  const [description, setDescription] = useState(kb.description ?? '');
  const [saved, setSaved] = useState(false);
  const updateError = update.error === undefined ? undefined : toApiError(update.error);
  const fields = fieldErrorsByName(updateError);
  const otherError = updateError !== undefined && Object.keys(fields).length === 0 ? updateError : undefined;
  const actionError = reindex.error ?? remove.error;
  const dirty = name.trim() !== kb.name || description.trim() !== (kb.description ?? '');

  const onSubmit = async (event: FormEvent) => {
    event.preventDefault();
    setSaved(false);
    try {
      const trimmed = description.trim();
      await updateKnowledgeBase({
        id: kb.id,
        name: name.trim(),
        description: trimmed === '' ? null : trimmed,
      }).unwrap();
      setSaved(true);
    } catch {
      // Shown below.
    }
  };

  const onDelete = async () => {
    try {
      await deleteKnowledgeBase(kb.id).unwrap();
      void navigate('/knowledge-bases', { replace: true });
    } catch {
      // Shown below.
    }
  };

  return (
    <Card>
      <CardHeader pb={0}>
        <Heading size="md">Settings</Heading>
      </CardHeader>
      <CardBody>
        <Stack spacing={6}>
          <form onSubmit={(event) => void onSubmit(event)} noValidate>
            <Stack spacing={4}>
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
              <FormControl isInvalid={fields.description !== undefined}>
                <FormLabel>Description</FormLabel>
                <Textarea
                  value={description}
                  onChange={(event) => {
                    setDescription(event.target.value);
                  }}
                />
                <FormErrorMessage>{fields.description}</FormErrorMessage>
                <FormHelperText>
                  The embedding profile cannot change: another profile needs a new knowledge base.
                </FormHelperText>
              </FormControl>
              {otherError !== undefined && (
                <ApiErrorAlert error={withMessage(otherError)} title="Not saved" />
              )}
              {saved && !dirty && (
                <Alert status="success" borderRadius="md">
                  <AlertIcon />
                  <AlertDescription>Saved.</AlertDescription>
                </Alert>
              )}
              <Button
                type="submit"
                colorScheme="brand"
                alignSelf="flex-start"
                isLoading={update.isLoading}
                isDisabled={!dirty || name.trim() === ''}
              >
                Save
              </Button>
            </Stack>
          </form>
          <Stack spacing={2}>
            <Text fontWeight="medium">Reindex</Text>
            <Text fontSize="sm" color="gray.500">
              Processes every document again from its stored file. Documents already in the queue keep their
              job.
            </Text>
            <Button
              size="sm"
              variant="outline"
              alignSelf="flex-start"
              isLoading={reindex.isLoading}
              onClick={() => void reindexKnowledgeBase(kb.id)}
            >
              Reindex all documents
            </Button>
            {reindex.data !== undefined && (
              <Text fontSize="sm" data-testid="reindex-result">
                {reindex.data.jobs_created} queued, {reindex.data.already_queued} already in the queue,{' '}
                {reindex.data.without_version} without a file.
              </Text>
            )}
          </Stack>
          {canDelete && (
            <Stack spacing={2}>
              <Text fontWeight="medium">Delete</Text>
              <Text fontSize="sm" color="gray.500">
                Removes the knowledge base, its documents and its search index. Assistants lose it at once.
              </Text>
              <HStack>
                <ConfirmButton
                  label="Delete knowledge base"
                  confirmLabel="Delete for good"
                  isLoading={remove.isLoading}
                  onConfirm={() => void onDelete()}
                />
              </HStack>
            </Stack>
          )}
          {actionError !== undefined && (
            <ApiErrorAlert error={withMessage(toApiError(actionError))} title="Not done" />
          )}
        </Stack>
      </CardBody>
    </Card>
  );
}

interface Candidate {
  id: string;
  name: string;
  detail: string | null;
}

function PrincipalSearch({
  type,
  onAdd,
  taken,
}: {
  type: PrincipalType;
  onAdd: (candidate: Candidate) => void;
  taken: (id: string) => boolean;
}) {
  const [q, setQ] = useState('');
  const term = q.trim();
  const users = useListUsersQuery({ q: term, limit: SEARCH_LIMIT }, { skip: type !== 'user' });
  const groups = useListGroupsQuery({ q: term, limit: SEARCH_LIMIT }, { skip: type !== 'group' });
  const result = type === 'user' ? users : groups;
  const candidates: Candidate[] =
    type === 'user'
      ? (users.data?.items ?? [])
          .filter((user) => user.status !== 'disabled')
          .map((user) => ({ id: user.id, name: user.display_name, detail: user.email }))
      : (groups.data?.items ?? []).map((group) => ({ id: group.id, name: group.name, detail: null }));

  return (
    <Stack spacing={2}>
      <Input
        aria-label={type === 'user' ? 'Find a user' : 'Find a group'}
        placeholder={type === 'user' ? 'Name or email' : 'Group name'}
        value={q}
        onChange={(event) => {
          setQ(event.target.value);
        }}
      />
      {result.isError && <ApiErrorAlert error={toApiError(result.error)} title="Cannot search" />}
      {result.data !== undefined && candidates.length === 0 && (
        <Text fontSize="sm" color="gray.500">
          Nothing found.
        </Text>
      )}
      <List spacing={1} data-testid="principal-candidates">
        {candidates.map((candidate) => (
          <ListItem key={candidate.id}>
            <HStack justify="space-between">
              <Text fontSize="sm">
                {candidate.name}
                {candidate.detail !== null && (
                  <Text as="span" color="gray.500">
                    {' '}
                    {candidate.detail}
                  </Text>
                )}
              </Text>
              <Button
                size="xs"
                aria-label={`Add ${candidate.name}`}
                isDisabled={taken(candidate.id)}
                onClick={() => {
                  onAdd(candidate);
                }}
              >
                {taken(candidate.id) ? 'Added' : 'Add'}
              </Button>
            </HStack>
          </ListItem>
        ))}
      </List>
    </Stack>
  );
}

const grantKey = (grant: { principal_type: PrincipalType; principal_id: string }) =>
  `${grant.principal_type}:${grant.principal_id}`;

/** Who can read or manage the knowledge base, edited as a whole list and saved in one request. */
export function KnowledgeBaseAccessCard({ kbId }: { kbId: string }) {
  const access = useGetKnowledgeBaseAccessQuery(kbId);
  const [replaceAccess, replace] = useReplaceKnowledgeBaseAccessMutation();
  const canFindUsers = useHasPermission(PERMISSIONS.usersRead);
  const canFindGroups = useHasPermission(PERMISSIONS.groupsRead);
  const [draft, setDraft] = useState<KbGrant[] | null>(null);
  const [type, setType] = useState<PrincipalType>(canFindUsers ? 'user' : 'group');
  const [saved, setSaved] = useState(false);
  const grants = draft ?? access.data?.items ?? [];

  const edit = (next: KbGrant[]) => {
    setSaved(false);
    replace.reset();
    setDraft(next);
  };

  const onSave = async () => {
    try {
      await replaceAccess({
        id: kbId,
        grants: grants.map(({ principal_type, principal_id, access_level }) => ({
          principal_type,
          principal_id,
          access_level,
        })),
      }).unwrap();
      setDraft(null);
      setSaved(true);
    } catch {
      // Shown below.
    }
  };

  let body;
  if (access.isLoading) {
    body = <PageSpinner />;
  } else if (access.isError) {
    body = <ApiErrorAlert error={toApiError(access.error)} title="Cannot load access" />;
  } else {
    body = (
      <Stack spacing={4}>
        {grants.length === 0 ? (
          <Text color="gray.500">
            Nobody has access through this list. Users with the knowledge_bases.manage permission still manage
            it.
          </Text>
        ) : (
          <TableContainer>
            <Table size="sm">
              <Thead>
                <Tr>
                  <Th>Who</Th>
                  <Th>Access</Th>
                  <Th />
                </Tr>
              </Thead>
              <Tbody>
                {grants.map((grant) => (
                  <Tr key={grantKey(grant)}>
                    <Td whiteSpace="normal">
                      <HStack>
                        <Badge>{grant.principal_type}</Badge>
                        <Text as="span">{grant.name}</Text>
                        {grant.email !== null && (
                          <Text as="span" color="gray.500" fontSize="sm">
                            {grant.email}
                          </Text>
                        )}
                      </HStack>
                    </Td>
                    <Td>
                      <Select
                        size="sm"
                        aria-label={`Access of ${grant.name}`}
                        value={grant.access_level}
                        onChange={(event) => {
                          const level = event.target.value as AccessLevel;
                          edit(
                            grants.map((item) =>
                              grantKey(item) === grantKey(grant) ? { ...item, access_level: level } : item,
                            ),
                          );
                        }}
                      >
                        <option value="read">read</option>
                        <option value="manage">manage</option>
                      </Select>
                    </Td>
                    <Td>
                      <Button
                        size="sm"
                        variant="ghost"
                        colorScheme="red"
                        aria-label={`Remove ${grant.name}`}
                        onClick={() => {
                          edit(grants.filter((item) => grantKey(item) !== grantKey(grant)));
                        }}
                      >
                        Remove
                      </Button>
                    </Td>
                  </Tr>
                ))}
              </Tbody>
            </Table>
          </TableContainer>
        )}
        {canFindUsers || canFindGroups ? (
          <Stack spacing={2}>
            <FormControl>
              <FormLabel>Add</FormLabel>
              <Select
                aria-label="Principal type"
                value={type}
                onChange={(event) => {
                  setType(event.target.value as PrincipalType);
                }}
              >
                {canFindUsers && <option value="user">User</option>}
                {canFindGroups && <option value="group">Group</option>}
              </Select>
            </FormControl>
            <PrincipalSearch
              type={type}
              taken={(id) => grants.some((grant) => grantKey(grant) === `${type}:${id}`)}
              onAdd={(candidate) => {
                edit([
                  ...grants,
                  {
                    principal_type: type,
                    principal_id: candidate.id,
                    name: candidate.name,
                    email: type === 'user' ? candidate.detail : null,
                    access_level: 'read',
                    granted_by: null,
                    granted_at: null,
                  },
                ]);
              }}
            />
          </Stack>
        ) : (
          <Text fontSize="sm" color="gray.500">
            Adding people needs the users.read or groups.read permission.
          </Text>
        )}
        {replace.error !== undefined && (
          <Alert status="error" borderRadius="md" role="alert">
            <AlertIcon />
            <AlertDescription>{errorMessage(replace.error)}</AlertDescription>
          </Alert>
        )}
        {saved && draft === null && (
          <Alert status="success" borderRadius="md">
            <AlertIcon />
            <AlertDescription>Access saved.</AlertDescription>
          </Alert>
        )}
        <HStack>
          <Button
            colorScheme="brand"
            isDisabled={draft === null}
            isLoading={replace.isLoading}
            onClick={() => void onSave()}
          >
            Save access
          </Button>
          <Button
            variant="ghost"
            isDisabled={draft === null}
            onClick={() => {
              replace.reset();
              setDraft(null);
            }}
          >
            Discard changes
          </Button>
        </HStack>
      </Stack>
    );
  }

  return (
    <Card>
      <CardHeader pb={0}>
        <Heading size="md">Access</Heading>
      </CardHeader>
      <CardBody>{body}</CardBody>
    </Card>
  );
}
